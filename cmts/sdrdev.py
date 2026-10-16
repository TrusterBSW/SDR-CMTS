"""The SDR under the radio process (cmts/radio.py), behind one small interface, so that other
devices than the B210 can carry the CMTS:

  UhdRadio    Ettus B200/B210 through UHD (the reference: everything measured so far).
  BladeRFRadio  Nuand bladeRF 2.0 micro (AD9361, like the B210) through libbladeRF, Nuand's own
              library, called directly (ctypes): no SoapySDR or other layer in between. TX and
              RX run at the same sample rate (no FPGA decimation: the upstream processes
              decimate); its sample counters are the ticks of the radio process.

Interface (times in seconds of the device clock, ticks at p["mcr"]):
  describe() -> str          one log line: rates, frequencies, gains
  warning() -> str or None   a remark for the log (RX frequency off target...)
  check() -> str or None     an error that forbids running (inexact rates...)
  open_streams()             the TX stream (and the RX one), calibrations done
  tx_spp                     samples per TX packet (USB reserve computations)
  set_time(t), time_now()    device clock
  start_rx(t)                RX stream from device time t
  recv(buf) -> (n, tick, overflow)  (samples may come with an overflow flag: placed by their tick)
  send(x, tick, first)       timed when tick is not None, the burst opened when first
  end_burst()
  tx_event(timeout) -> 'U' (underflow), 'L' (late), None
  stop_rx()
  close()                    (bladeRF: the device again free for another open in this process)
"""
import time

import numpy as np

# UHD runs the AD9361 TX quadrature calibration when the streams open (set_active_chains); it
# loops a test tone back through the RX analog filter, at ~1/8 of the master clock and up. With
# the RX filter at the upstream width (12.4 MHz), the tone is cut from 41.712 MHz on: the TX
# image (mirror of the signal around the LO) stayed at -21..-24 dBc instead of -51, the channel
# at the LO sat on its own mirror and the Sagem never locked above 34.76 MHz (2026-10-10). The
# streams open with this RX bandwidth, then the RX filter goes back to the upstream width.
TX_CAL_RX_BW = 56e6


def open_radio(p, has_rx):
    """The device of the radio parameters p (p["driver"]: "uhd", the default, or "bladerf")."""
    drv = p.get("driver", "uhd")
    if drv == "uhd":
        return UhdRadio(p, has_rx)
    if drv == "bladerf":
        return BladeRFRadio(p, has_rx)
    raise ValueError(f"pilote SDR inconnu : {drv}")


class UhdRadio:
    name = "B210"

    def __init__(self, p, has_rx):
        import uhd
        self.uhd, self.p, self.has_rx = uhd, p, has_rx
        self.usrp = usrp = uhd.usrp.MultiUSRP(p["args"])
        if p.get("clock_source", "internal") != "internal":
            # a common 10 MHz for every B210 (--clock-source external): their master clocks, hence
            # their symbol clocks, then never drift apart
            usrp.set_clock_source(p["clock_source"])
            t_lock = time.time() + 5
            while not usrp.get_mboard_sensor("ref_locked").to_bool():
                if time.time() > t_lock:
                    raise RuntimeError("référence 10 MHz externe non verrouillée (ref_locked) après 5 s")
                time.sleep(0.1)
        usrp.set_tx_rate(p["fs_tx"])
        usrp.set_tx_freq(uhd.types.TuneRequest(p["tx_center"]))
        usrp.set_tx_bandwidth(p["tx_bw"])
        usrp.set_tx_gain(p["gain"])
        usrp.set_tx_antenna("TX/RX")
        if has_rx:
            usrp.set_rx_rate(p["mcr"] / p["decim"])
            if p["rx_lo_off"]:
                usrp.set_rx_freq(uhd.types.TuneRequest(p["rx_target"], p["rx_lo_off"]))
            else:
                usrp.set_rx_freq(uhd.types.TuneRequest(p["rx_target"]))
            usrp.set_rx_bandwidth(p["rx_bw"])
            usrp.set_rx_gain(p["rx_gain"])
            usrp.set_rx_antenna("RX2")
        self.tx_stream = self.rx_stream = None
        self.tx_spp = 0
        self._md = uhd.types.TXMetadata()
        self._rmd = uhd.types.RXMetadata()
        self._amd = uhd.types.TXAsyncMetadata()

    def describe(self):
        u, p = self.usrp, self.p
        return (f"master clock {u.get_master_clock_rate() / 1e6:.6f} MHz | TX {u.get_tx_rate() / 1e6:.6f} MS/s "
                f"@ {u.get_tx_freq() / 1e6:.3f} MHz gain {u.get_tx_gain()} dB BW {u.get_tx_bandwidth() / 1e6:.0f} MHz"
                + (f" | RX {u.get_rx_rate() / 1e6:.6f} MS/s @ {u.get_rx_freq() / 1e6:.4f} MHz "
                   f"(LO {(p['rx_target'] + p['rx_lo_off']) / 1e6:.1f} MHz) gain {u.get_rx_gain()} dB"
                   if self.has_rx else " | émission seule")
                + f" | horloge {p.get('clock_source', 'internal')}")

    def warning(self):
        u, p = self.usrp, self.p
        if self.has_rx and abs(u.get_rx_freq() - p["rx_target"]) > 1e3:
            return f"!! fréquence RX obtenue {u.get_rx_freq() / 1e6:.4f} MHz au lieu de {p['rx_target'] / 1e6:.4f}"
        return None

    def check(self):
        u, p = self.usrp, self.p
        if abs(u.get_tx_rate() - p["fs_tx"]) / p["fs_tx"] > 1e-6 or (
                self.has_rx and abs(u.get_rx_rate() * p["decim"] - p["mcr"]) > 1):
            return "débits inexacts: la base de temps commune TX/RX serait fausse"
        return None

    def open_streams(self):
        uhd, u = self.uhd, self.usrp
        tx_args = uhd.usrp.StreamArgs("fc32", "sc16")
        tx_args.args = "underflow_policy=next_packet"
        u.set_rx_bandwidth(TX_CAL_RX_BW)       # UHD calibrates the TX quadrature as the streams open
        self.tx_stream = u.get_tx_stream(tx_args)
        self.rx_stream = u.get_rx_stream(uhd.usrp.StreamArgs("fc32", "sc16")) if self.has_rx else None
        if self.has_rx:
            u.set_rx_bandwidth(self.p["rx_bw"])  # back to the upstream width (TX_CAL_RX_BW)
        self.tx_spp = self.tx_stream.get_max_num_samps()
        self.rx_spp = self.rx_stream.get_max_num_samps() if self.rx_stream is not None else 0

    def set_time(self, t):
        self.usrp.set_time_now(self.uhd.types.TimeSpec(t))

    def close(self):
        """Streams, then the device, released while the process still runs: left to the
        interpreter's exit, UHD's threads were destroyed still running ("terminate called without
        an active exception" at every stop)."""
        self.tx_stream = self.rx_stream = None
        self.usrp = None
        import gc
        gc.collect()

    def time_now(self):
        return self.usrp.get_time_now().get_real_secs()

    def start_rx(self, t):
        cmd = self.uhd.types.StreamCMD(self.uhd.types.StreamMode.start_cont)
        cmd.stream_now = False
        cmd.time_spec = self.uhd.types.TimeSpec(t)
        self.rx_stream.issue_stream_cmd(cmd)

    def stop_rx(self):
        if self.rx_stream is not None:
            self.rx_stream.issue_stream_cmd(self.uhd.types.StreamCMD(self.uhd.types.StreamMode.stop_cont))

    def recv(self, buf):
        """buf: (1, n) complex64. -> (samples, tick of the first, overflow)."""
        md = self._rmd
        n = self.rx_stream.recv(buf, md, 0.5)
        ec = md.error_code
        if ec == self.uhd.types.RXMetadataErrorCode.overflow:
            return 0, None, True
        if ec != self.uhd.types.RXMetadataErrorCode.none or not n:
            return 0, None, False
        mcr = self.p["mcr"]
        return n, md.time_spec.get_full_secs() * mcr + round(md.time_spec.get_frac_secs() * mcr), False

    def send(self, x, tick, first):
        md = self._md
        md.start_of_burst = first
        md.end_of_burst = False
        if tick is not None:
            md.has_time_spec = True
            md.time_spec = self.uhd.types.TimeSpec.from_ticks(tick, self.p["mcr"])
        else:
            md.has_time_spec = False
        sent = 0
        while sent < x.size:
            sent += self.tx_stream.send(x[sent:], md, 1.0)
            md.start_of_burst = False
            md.has_time_spec = False

    def end_burst(self):
        md = self._md
        md.start_of_burst, md.has_time_spec, md.end_of_burst = False, False, True
        self.tx_stream.send(np.zeros(1000, dtype=np.complex64), md)

    def tx_event(self, timeout):
        if not self.tx_stream.recv_async_msg(self._amd, timeout):
            return None
        ec, C = self._amd.event_code, self.uhd.types.TXMetadataEventCode
        if ec in (C.underflow, C.underflow_in_packet):
            return "U"
        if ec == C.time_error:
            return "L"
        return None


# ------------------------------------------------------------------ bladeRF (libbladeRF, ctypes)
_BRF = None
CH_RX0, CH_TX0 = 0, 1                     # BLADERF_CHANNEL_RX(0), BLADERF_CHANNEL_TX(0)
RX_X1, TX_X1 = 0, 1                       # bladerf_channel_layout
DIR_RX, DIR_TX = 0, 1                     # bladerf_direction
GAIN_MGC = 1                              # bladerf_gain_mode: BLADERF_GAIN_DEFAULT, BLADERF_GAIN_MGC
META_TX_BURST_START, META_TX_BURST_END, META_TX_UPDATE_TIMESTAMP = 1 << 0, 1 << 1, 1 << 3
META_TX_NOW = 1 << 2
META_RX_NOW = 1 << 31
META_STATUS_OVERRUN, META_STATUS_UNDERRUN = 1 << 0, 1 << 1
BLADERF_ERR_TIME_PAST = -14
BLADERF_ERR_NODEV = -7
Q11 = 2048.0                              # SC16 Q11: +-2048 full scale
BLADERF2_RX_MIN_HZ = 70e6                 # bladeRF 2.0 micro: RX from 70 MHz (TX from 47 MHz)


def _lib():
    """libbladeRF with the prototypes used here; (lib, (major, minor, patch))."""
    global _BRF
    if _BRF is not None:
        return _BRF
    import ctypes
    import ctypes.util
    name = ctypes.util.find_library("bladeRF") or "libbladeRF.so.2"
    lib = ctypes.CDLL(name)
    c = ctypes
    P = c.c_void_p

    class Version(c.Structure):
        _fields_ = [("major", c.c_uint16), ("minor", c.c_uint16), ("patch", c.c_uint16), ("describe", c.c_char_p)]

    class Range(c.Structure):
        _fields_ = [("min", c.c_int64), ("max", c.c_int64), ("step", c.c_int64), ("scale", c.c_float)]

    class Meta(c.Structure):
        _fields_ = [("timestamp", c.c_uint64), ("flags", c.c_uint32), ("status", c.c_uint32),
                    ("actual_count", c.c_uint), ("reserved", c.c_uint8 * 32)]

    protos = {
        "bladerf_open": (c.c_int, [c.POINTER(P), c.c_char_p]),
        "bladerf_close": (None, [P]),
        "bladerf_get_board_name": (c.c_char_p, [P]),
        "bladerf_get_serial": (c.c_int, [P, c.c_char_p]),
        "bladerf_set_sample_rate": (c.c_int, [P, c.c_int, c.c_uint, c.POINTER(c.c_uint)]),
        "bladerf_get_sample_rate": (c.c_int, [P, c.c_int, c.POINTER(c.c_uint)]),
        "bladerf_set_bandwidth": (c.c_int, [P, c.c_int, c.c_uint, c.POINTER(c.c_uint)]),
        "bladerf_set_frequency": (c.c_int, [P, c.c_int, c.c_uint64]),
        "bladerf_get_frequency": (c.c_int, [P, c.c_int, c.POINTER(c.c_uint64)]),
        "bladerf_set_gain_mode": (c.c_int, [P, c.c_int, c.c_int]),
        "bladerf_set_gain": (c.c_int, [P, c.c_int, c.c_int]),
        "bladerf_get_gain": (c.c_int, [P, c.c_int, c.POINTER(c.c_int)]),
        "bladerf_get_gain_range": (c.c_int, [P, c.c_int, c.POINTER(c.POINTER(Range))]),
        "bladerf_sync_config": (c.c_int, [P, c.c_int, c.c_int, c.c_uint, c.c_uint, c.c_uint, c.c_uint]),
        "bladerf_enable_module": (c.c_int, [P, c.c_int, c.c_bool]),
        "bladerf_get_timestamp": (c.c_int, [P, c.c_int, c.POINTER(c.c_uint64)]),
        "bladerf_sync_tx": (c.c_int, [P, c.c_void_p, c.c_uint, c.POINTER(Meta), c.c_uint]),
        "bladerf_sync_rx": (c.c_int, [P, c.c_void_p, c.c_uint, c.POINTER(Meta), c.c_uint]),
        "bladerf_strerror": (c.c_char_p, [c.c_int]),
        "bladerf_version": (None, [c.POINTER(Version)]),
        "bladerf_fpga_version": (c.c_int, [P, c.POINTER(Version)]),
    }
    for fn, (res, args) in protos.items():
        f = getattr(lib, fn)
        f.restype, f.argtypes = res, args
    v = Version()
    lib.bladerf_version(c.byref(v))
    lib.Meta, lib.Range, lib.Version = Meta, Range, Version
    _BRF = (lib, (v.major, v.minor, v.patch))
    return _BRF


def bladerf_meta_format(version):
    """BLADERF_FORMAT_SC16_Q11_META: 1 up to libbladeRF 2.5 (Debian 12: 2.4.1), 2 from 2.6, where
    BLADERF_FORMAT_SC16_Q11_PACKED was inserted before it (the enum has no fixed values)."""
    import os
    if os.environ.get("MINI_CMTS_BLADERF_META_FORMAT"):      # diagnostic override
        return int(os.environ["MINI_CMTS_BLADERF_META_FORMAT"])
    return 2 if tuple(version[:2]) >= (2, 6) else 1


def fpga_mismatch(lib_version, fpga_version):
    """An FPGA newer than the library knows: libbladeRF 2.4.1 (Debian 12) with the FPGA 0.16.0 of
    the flash (bladeRF 2025.10) read RX buffers of 508 samples whose timestamps moved by 2044,
    every read flagged as an overrun, the RX at 7 MS/s of 27.8 (2026-10-15). With the FPGA 0.14 of
    the same Debian release loaded: whole buffers, contiguous timestamps."""
    if fpga_version is None or tuple(lib_version[:2]) >= (2, 6) or tuple(fpga_version[:2]) < (0, 16):
        return None
    return (f"bladeRF : FPGA {'.'.join(map(str, fpga_version))} trop récent pour libbladeRF "
            f"{'.'.join(map(str, lib_version))} (métadonnées des flux mal lues : réception fausse). "
            f"Charger le FPGA de cette version de la bibliothèque (jusqu'à la coupure de courant) : "
            f"bladeRF-cli -l /usr/share/Nuand/bladeRF/hostedxA4.rbf ; ou installer libbladeRF >= 2.6")


class BladeRFRadio:
    """A Nuand bladeRF 2.0 micro. p["args"]: the libbladeRF device identifier ("" for the first
    one, "*:serial=<serial>"); TX on TX1, RX on RX1. Its sample counters cannot be set: the
    device time is the TX counter plus an offset kept here (set_time), and the RX counter is
    tied to it by a reading of both taken once both modules run."""

    def __init__(self, p, has_rx):
        import ctypes
        self.c = ctypes
        self.lib, self.version = _lib()
        self.p, self.has_rx = p, has_rx
        self.fmt = bladerf_meta_format(self.version)
        self.dev = ctypes.c_void_p()
        ident = p.get("args", "") or ""
        for attempt in range(10):          # (closed a moment ago, the device is reset: NODEV for ~1 s)
            ret = self.lib.bladerf_open(ctypes.byref(self.dev), ident.encode() if ident else None)
            if ret != BLADERF_ERR_NODEV:
                break
            time.sleep(0.5)
        self._ok(ret, f"ouverture « {ident or 'premier bladeRF'} »")
        self.board = (self.lib.bladerf_get_board_name(self.dev) or b"?").decode()
        fv = self.lib.Version()
        self.fpga = (fv.major, fv.minor, fv.patch) if self.lib.bladerf_fpga_version(self.dev, ctypes.byref(fv)) == 0 else None
        err = fpga_mismatch(self.version, self.fpga)
        if err:
            raise RuntimeError(err)
        sn = ctypes.create_string_buffer(64)
        self.serial = sn.value.decode() if self.lib.bladerf_get_serial(self.dev, sn) == 0 else "?"
        self.name = f"{self.board} {self.serial[:12]}"
        if self.board == "bladerf1" and has_rx and p["rx_target"] < 300e6:
            raise RuntimeError("bladeRF 1 (LMS6002D) : réception de 300 MHz à 3,8 GHz seulement, le montant "
                               f"({p['rx_target'] / 1e6:.1f} MHz) demande le transverter XB-200")
        fs = int(round(p["fs_tx"]))
        if abs(fs - p["fs_tx"]) > 1e-3:
            raise ValueError("bladeRF : débit d'échantillons entier en Hz seulement")
        act = ctypes.c_uint()
        L = self.lib
        self._ok(L.bladerf_set_sample_rate(self.dev, CH_TX0, fs, ctypes.byref(act)), "débit TX")
        self._ok(L.bladerf_set_bandwidth(self.dev, CH_TX0, int(p["tx_bw"]), ctypes.byref(act)), "bande TX")
        self._ok(L.bladerf_set_frequency(self.dev, CH_TX0, int(round(p["tx_center"]))), "fréquence TX")
        self._ok(L.bladerf_set_gain(self.dev, CH_TX0, int(round(p["gain"]))), "gain TX")
        if has_rx:
            if p["decim"] != 1:
                raise ValueError("bladeRF : réception au débit de l'émission seulement (décimation 1)")
            self._ok(L.bladerf_set_sample_rate(self.dev, CH_RX0, fs, ctypes.byref(act)), "débit RX")
            self._ok(L.bladerf_set_frequency(self.dev, CH_RX0, int(round(p["rx_target"] + p["rx_lo_off"]))),
                     "fréquence RX")
            self._ok(L.bladerf_set_bandwidth(self.dev, CH_RX0, int(p["rx_bw"]), ctypes.byref(act)), "bande RX")
            self._ok(L.bladerf_set_gain_mode(self.dev, CH_RX0, GAIN_MGC), "gain RX manuel")
            self._ok(L.bladerf_set_gain(self.dev, CH_RX0, int(round(p["rx_gain"]))), "gain RX")
        self.base = 0                     # device time = (TX counter + base) / fs, in samples
        self.rx_to_tx = 0                 # TX counter - RX counter, read once both run
        self.tx_spp = self.rx_spp = 0
        self.fs = fs
        self._events = []
        self._md = L.Meta()
        self._rmd = L.Meta()
        self._ibuf = None
        self._rx_next = None              # RX counter value the next read asks for
        self._rx_start = 0                # device tick of the first sample to hand out (start_rx)

    def _ok(self, ret, what):
        if ret < 0:
            raise RuntimeError(f"bladeRF {what} : {(self.lib.bladerf_strerror(ret) or b'?').decode()} ({ret})")
        return ret

    def _get(self, fn, ch, ctype):
        v = ctype()
        fn(self.dev, ch, self.c.byref(v))
        return v.value

    def describe(self):
        L, c = self.lib, self.c
        ver = ".".join(map(str, self.version))
        fpga = ".".join(map(str, self.fpga)) if self.fpga else "?"
        s = (f"{self.name} (libbladeRF {ver}, FPGA {fpga}) | TX {self._get(L.bladerf_get_sample_rate, CH_TX0, c.c_uint) / 1e6:.6f} MS/s "
             f"@ {self._get(L.bladerf_get_frequency, CH_TX0, c.c_uint64) / 1e6:.3f} MHz "
             f"gain {self._get(L.bladerf_get_gain, CH_TX0, c.c_int)} dB")
        if self.has_rx:
            s += (f" | RX {self._get(L.bladerf_get_sample_rate, CH_RX0, c.c_uint) / 1e6:.6f} MS/s "
                  f"@ {self._get(L.bladerf_get_frequency, CH_RX0, c.c_uint64) / 1e6:.4f} MHz "
                  f"gain {self._get(L.bladerf_get_gain, CH_RX0, c.c_int)} dB")
        else:
            s += " | émission seule"
        return s + f" | horloge {self.p.get('clock_source', 'internal')}"

    def warning(self):
        if self.p.get("clock_source", "internal") != "internal":
            return "!! bladeRF : référence externe non gérée ici (horloge interne)"
        return None

    def check(self):
        """Rates within 1 ppm (27,808,000 Hz asked, 27,807,999 obtained: 0.04 ppm, far below the
        oscillator itself), the same both ways."""
        L, c = self.lib, self.c
        tx = self._get(L.bladerf_get_sample_rate, CH_TX0, c.c_uint)
        if abs(tx - self.fs) > 1e-6 * self.fs:
            return f"bladeRF : débit TX obtenu {tx} au lieu de {self.fs}"
        if self.has_rx and self._get(L.bladerf_get_sample_rate, CH_RX0, c.c_uint) != tx:
            return "bladeRF : débits TX et RX différents, la base de temps commune serait fausse"
        return None

    def open_streams(self):
        L = self.lib
        nbuf, blen, nxfer = 64, 16384, 32       # deep USB buffering, as the B210's 48 x 16 KB frames
        self._ok(L.bladerf_sync_config(self.dev, TX_X1, self.fmt, nbuf, blen, nxfer, 1000), "flux TX")
        self._ok(L.bladerf_enable_module(self.dev, CH_TX0, True), "activation TX")
        # (libbladeRF 2.4.1, bladeRF 2.0: a gain set before its module is enabled is answered with
        # success and ignored, the RX stayed at its 60 dB; set again once the modules run)
        self._ok(L.bladerf_set_gain(self.dev, CH_TX0, int(round(self.p["gain"]))), "gain TX")
        self.tx_spp = blen
        if self.has_rx:
            self._ok(L.bladerf_sync_config(self.dev, RX_X1, self.fmt, nbuf, blen, nxfer, 1000), "flux RX")
            self._ok(L.bladerf_enable_module(self.dev, CH_RX0, True), "activation RX")
            self._ok(L.bladerf_set_gain_mode(self.dev, CH_RX0, GAIN_MGC), "gain RX manuel")
            self._ok(L.bladerf_set_gain(self.dev, CH_RX0, int(round(self.p["rx_gain"]))), "gain RX")
            self.rx_spp = blen
            # TX - RX counter: the RX read between two TX reads (shortest of 8). Read back to back,
            # TX then RX, the USB round trip itself (~165 us) came out as an offset: -4581 samples,
            # where the loop test found the counters equal to ~3 us (2026-10-15)
            best = None
            for _ in range(8):
                t0 = time.perf_counter()
                tx1 = self._counter(DIR_TX)
                rx = self._counter(DIR_RX)
                tx2 = self._counter(DIR_TX)
                d = time.perf_counter() - t0
                if best is None or d < best[0]:
                    best = (d, (tx1 + tx2) // 2 - rx)
            self.rx_to_tx = best[1]
        c = self.c
        self.note = (f"gains en place : TX {self._get(L.bladerf_get_gain, CH_TX0, c.c_int)} dB"
                     + (f", RX {self._get(L.bladerf_get_gain, CH_RX0, c.c_int)} dB ; compteurs TX - RX "
                        f"{self.rx_to_tx} éch." if self.has_rx else ""))

    def _counter(self, direction):
        v = self.c.c_uint64()
        self._ok(self.lib.bladerf_get_timestamp(self.dev, direction, self.c.byref(v)), "horodatage")
        return v.value

    def set_time(self, t):
        self.base = int(round(t * self.fs)) - self._counter(DIR_TX)

    def time_now(self):
        return (self._counter(DIR_TX) + self.base) / self.fs

    def start_rx(self, t):
        """Samples before device time t are dropped (the B210 starts its RX stream at t; here the
        RX runs from open_streams, and its first buffers predate set_time: negative ticks, the
        upstream processes waited for a time 0 that never came, 2026-10-15)."""
        self._rx_start = int(round(t * self.fs))

    def stop_rx(self):
        if self.has_rx:
            self.lib.bladerf_enable_module(self.dev, CH_RX0, False)

    def recv(self, buf):
        """Whole buffers, back to back: each read asks for the timestamp that follows the last
        one, and libbladeRF waits until all of it is there. RX_NOW only to start and after a loss
        (that timestamp already gone): read with RX_NOW every time, each call returned the ~50
        samples already in and the RX fell to 7 MS/s of 27.8, every read flagged as a gap."""
        n = buf.shape[1]
        if self._ibuf is None or self._ibuf.size != 2 * n:
            self._ibuf = np.empty(2 * n, np.int16)
        md = self._rmd
        md.status, md.actual_count = 0, 0
        if self._rx_next is None:
            md.flags, md.timestamp = META_RX_NOW, 0
        else:
            md.flags, md.timestamp = 0, self._rx_next
        ret = self.lib.bladerf_sync_rx(self.dev, self._ibuf.ctypes.data, n, self.c.byref(md), 500)
        if ret == BLADERF_ERR_TIME_PAST:
            self._rx_next = None                # samples lost: start again from now
            return 0, None, True
        if ret < 0:
            self._rx_next = None
            return 0, None, False
        k = md.actual_count or (n if self._rx_next is not None else 0)
        overrun = bool(md.status & META_STATUS_OVERRUN)
        if not k:
            return 0, None, overrun
        self._rx_next = md.timestamp + k
        tick = md.timestamp + self.rx_to_tx + self.base
        skip = max(0, min(k, self._rx_start - tick))
        if skip == k:
            return 0, None, False
        k -= skip
        buf[0, :k] = (self._ibuf[2 * skip:2 * (skip + k)].astype(np.float32) * (1.0 / Q11)).view(np.complex64)
        return k, tick + skip, overrun

    def send(self, x, tick, first):
        v = np.empty(2 * x.size, np.int16)
        np.multiply(x.view(np.float32), Q11 - 1, out=v, casting="unsafe")
        md = self._md
        md.status = 0
        if first:
            md.flags = META_TX_BURST_START
        elif tick is not None:
            md.flags = META_TX_UPDATE_TIMESTAMP     # zero-padded up to it if there is a gap
        else:
            md.flags = 0
        if tick is not None:
            ts = tick - self.base
            if ts < 0:
                raise RuntimeError("bladeRF : horodatage d'émission avant le zéro du compteur")
            md.timestamp = ts
        self._ok(self.lib.bladerf_sync_tx(self.dev, v.ctypes.data, x.size, self.c.byref(md), 1000), "émission")
        if md.status & META_STATUS_UNDERRUN:
            self._events.append("U")

    def end_burst(self):
        md = self._md
        md.flags, md.status = META_TX_BURST_END, 0
        z = np.zeros(2 * 1024, np.int16)
        self.lib.bladerf_sync_tx(self.dev, z.ctypes.data, 1024, self.c.byref(md), 1000)
        self.lib.bladerf_enable_module(self.dev, CH_TX0, False)

    def close(self):
        if self.dev:
            self.lib.bladerf_close(self.dev)
            self.dev = self.c.c_void_p()

    def tx_event(self, timeout):
        if self._events:
            return self._events.pop(0)
        time.sleep(timeout)                 # (libbladeRF's sync interface reports underruns per call)
        return None
