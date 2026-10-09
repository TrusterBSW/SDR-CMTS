#!/usr/bin/env python3
"""EuroDOCSIS downstream test transmitter (J.83 Annex A + SYNC/UCD/MAP) for a USRP B210.

Goal: make a real cable modem lock on the downstream and start ranging
(you then see its RNG-REQ bursts on the upstream frequency with a HackRF / RTL-SDR).

  ./ds_tx.py --freq 474e6 --gain 40            # transmit with the B210
  ./ds_tx.py --bench                           # measure generation speed only
  ./ds_tx.py --out ds.cfile --seconds 2        # write complex64 samples to a file

NEVER connect this to the operator's network: closed coax lab only, with attenuators.
"""
import argparse
import queue
import sys
import threading
import time

import numpy as np

from cmts.downstream import DownstreamMux, SYMRATE

BLOCK_PKTS = 256  # ~10 ms at 64-QAM


def build_mux(a):
    return DownstreamMux(qam=a.qam, sps=a.sps, us_freq=a.us_freq, us_symrate_mult=a.us_rate,
                         minislot_ticks=a.minislot, cmts_mac=a.mac, invert=not a.no_invert)


def encoder_thread(mux, q, stop):
    """TS scheduling + J.83A coding -> symbols."""
    while not stop.is_set():
        q.put(mux.enc.encode(mux.packets(BLOCK_PKTS)))


def shaper_thread(mux, qin, qout, scale, stop):
    """RRC pulse shaping (C code, runs without the GIL) -> scaled samples."""
    while not stop.is_set():
        x = mux.shape(qin.get()) * scale
        np.clip(x.real, -1, 1, out=x.real)
        np.clip(x.imag, -1, 1, out=x.imag)
        qout.put(x)


def run_bench(a):
    mux = build_mux(a)
    mux.samples(BLOCK_PKTS)  # warm up
    n = 40
    t0 = time.perf_counter()
    for _ in range(n):
        x = mux.samples(BLOCK_PKTS)
    dt = time.perf_counter() - t0
    sec = n * x.size / (a.sps * SYMRATE)
    print(f"generated {sec:.3f} s of signal in {dt:.3f} s -> {sec / dt:.2f}x real time")
    print("stats:", mux.stats)


def run_file(a):
    mux = build_mux(a)
    fs = a.sps * SYMRATE
    total = int(a.seconds * fs)
    done = 0
    with open(a.out, "wb") as f:
        while done < total:
            x = (mux.samples(BLOCK_PKTS) * a.scale).astype(np.complex64)
            f.write(x.tobytes())
            done += x.size
    print(f"wrote {done} samples @ {fs / 1e6:.3f} MS/s to {a.out}; stats: {mux.stats}")


def run_usrp(a):
    import uhd
    fs = a.sps * SYMRATE
    mcr = fs * a.mcr_mult
    usrp = uhd.usrp.MultiUSRP(f"{a.args},master_clock_rate={mcr}")
    usrp.set_tx_rate(fs)
    usrp.set_tx_freq(uhd.types.TuneRequest(a.freq))
    usrp.set_tx_gain(a.gain)
    usrp.set_tx_antenna("TX/RX")
    print(f"master clock {usrp.get_master_clock_rate() / 1e6:.6f} MHz, "
          f"tx rate {usrp.get_tx_rate() / 1e6:.6f} MS/s (wanted {fs / 1e6:.6f}), "
          f"freq {usrp.get_tx_freq() / 1e6:.3f} MHz, gain {usrp.get_tx_gain()} dB")
    err = (usrp.get_tx_rate() - fs) / fs * 1e6
    if abs(err) > 1:
        print(f"WARNING: sample rate error {err:.2f} ppm (DOCSIS needs < 5 ppm)")
    streamer = usrp.get_tx_stream(uhd.usrp.StreamArgs("fc32", "sc16"))

    mux = build_mux(a)
    qsym = queue.Queue(maxsize=8)
    q = queue.Queue(maxsize=16)
    stop = threading.Event()
    for target, args in ((encoder_thread, (mux, qsym, stop)),
                         (shaper_thread, (mux, qsym, q, a.scale, stop))):
        threading.Thread(target=target, args=args, daemon=True).start()
    while q.qsize() < 8:  # pre-fill
        time.sleep(0.01)

    md = uhd.types.TXMetadata()
    md.start_of_burst = True
    md.end_of_burst = False
    t_last = time.time()
    print("transmitting, Ctrl-C to stop")
    try:
        while True:
            x = q.get()
            sent = 0
            while sent < x.size:  # send() may return early on timeout
                sent += streamer.send(x[sent:], md, 1.0)
                md.start_of_burst = False
            if time.time() - t_last > 5:
                t_last = time.time()
                print(f"[{time.strftime('%H:%M:%S')}] queue={q.qsize()} stats={mux.stats}",
                      flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        md.end_of_burst = True
        streamer.send(np.zeros(1000, dtype=np.complex64), md)
        print("stopped; stats:", mux.stats)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--freq", type=float, default=474e6, help="downstream center frequency (Hz)")
    ap.add_argument("--gain", type=float, default=40, help="B210 TX gain dB (0..89.75)")
    ap.add_argument("--qam", type=int, default=64, choices=(64, 256))
    ap.add_argument("--sps", type=int, default=2, help="samples per symbol")
    ap.add_argument("--mcr-mult", type=int, default=2, help="master clock = sps*6.952M*this")
    ap.add_argument("--scale", type=float, default=0.3, help="digital amplitude (RMS, full scale = 1)")
    ap.add_argument("--us-freq", type=float, default=40e6, help="upstream center frequency announced in the UCD")
    ap.add_argument("--us-rate", type=int, default=8, help="upstream symbol rate in 160 ksym/s units (8 = 1.28 Msym/s)")
    ap.add_argument("--minislot", type=int, default=4, help="minislot size in 6.25 us ticks")
    ap.add_argument("--mac", default="02:00:00:ca:fe:01", help="CMTS MAC address")
    ap.add_argument("--no-invert", action="store_true",
                    help="do not mirror the spectrum (default mirrors it, like the SFR network)")
    ap.add_argument("--args", default="type=b200", help="UHD device args")
    ap.add_argument("--bench", action="store_true")
    ap.add_argument("--out", help="write samples to this file instead of transmitting")
    ap.add_argument("--seconds", type=float, default=1.0)
    a = ap.parse_args()
    if a.bench:
        run_bench(a)
    elif a.out:
        run_file(a)
    else:
        run_usrp(a)


if __name__ == "__main__":
    sys.exit(main())
