# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""Closed-loop ranging without hardware: DownstreamMux (real MAP / RNG-RSP bytes) ->
simulated modem (real RNG-REQ / REQ bursts as IQ) -> UpstreamRx -> CmtsCore -> RNG-RSP."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import sdr_cmts

CASES = [
    ("nominal", []),
    ("latence TX 150 ms", ["--sim-latency-ms", "150"]),
    ("modem en avance de 25 µs", ["--sim-delay-us", "-25"]),
    ("Sagem 3686 réel : 183 µs d'avance", ["--sim-delay-us", "-183", "--sim-freq", "260"]),
    ("modem 600 µs de retard", ["--sim-delay-us", "600"]),
    ("faible, bruité, -9 kHz", ["--sim-power", "-55", "--sim-snr", "12", "--sim-freq", "-9000"]),
    ("256-QAM, 2.56 Msym/s", ["--qam", "256", "--us-rate", "16"]),
]
ok_all = True
for name, extra in CASES:
    a = sdr_cmts.parser().parse_args(["--sim", "2.5"] + extra)
    r = sdr_cmts.run_sim(a, log=lambda *x: None)
    ok = (r["state"] == "ranged" and abs(r["residual_us"]) < 0.1 and abs(r["power"] - a.us_target) < 1
          and abs(r["freq"]) < 100 and r["t3"] == 0 and r["reqs"] == r["tx"]["REQ"] > 0
          and max(r["rsp_delays"]) < 200)
    ok_all &= ok
    print(f"{'OK ' if ok else 'KO '} {name:28s} success à {r['success_s'] or 0:.3f} s, résidu "
          f"{r['residual_us'] * 1e3:+6.1f} ns, P {r['power']:6.2f} dBFS, F {r['freq']:+5.0f} Hz, "
          f"RNG-REQ {r['tx']['RNG-REQ']}, REQ {r['reqs']}/{r['tx']['REQ']}, T3 {r['t3']}, "
          f"RSP {min(r['rsp_delays']):.0f}-{max(r['rsp_delays']):.0f} ms")
print("PASS" if ok_all else "FAIL")
sys.exit(0 if ok_all else 1)
