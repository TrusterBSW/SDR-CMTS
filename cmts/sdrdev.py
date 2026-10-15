"""The SDR under the radio process (cmts/radio.py), behind one small interface, so that other
devices than the B210 can carry the CMTS:

  UhdRadio    Ettus B200/B210 through UHD (the reference: everything measured so far).

Interface (times in seconds of the device clock, ticks at p["mcr"]):
  describe() -> str          one log line: rates, frequencies, gains
  warning() -> str or None   a remark for the log (RX frequency off target...)
  check() -> str or None     an error that forbids running (inexact rates...)
  open_streams()             the TX stream (and the RX one), calibrations done
  tx_spp                     samples per TX packet (USB reserve computations)
  set_time(t), time_now()    device clock
  start_rx(t)                RX stream from device time t
  recv(buf) -> (n, tick, overflow)
  send(x, tick, first)       timed when tick is not None, the burst opened when first
  end_burst()
  tx_event(timeout) -> 'U' (underflow), 'L' (late), None
  stop_rx()
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
    """The device of the radio parameters p (p["driver"]: "uhd", the default)."""
    drv = p.get("driver", "uhd")
    if drv == "uhd":
        return UhdRadio(p, has_rx)
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
