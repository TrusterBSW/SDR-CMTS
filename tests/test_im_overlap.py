# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""Burst attribution when a unicast grant lies inside the Initial Maintenance slack (--im-slack-us).

Seen on the Sagem 3686 on 2026-10-09: the MAP put the Station Maintenance grant 2 ms after the
IM start, inside the 1 ms slack that follows the 1.2 ms IM. The receiver measured the SM burst
against the IM, the CMTS sent a +2000 us "correction" and the modem was lost.
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import sdr_cmts
from cmts import docsis_mac as mac
from cmts.sim_modem import SimModem
from cmts.upstream import TS_RATE

MODEM = bytes.fromhex("247f20a838b8")
US = 1e-6 * TS_RATE


def run(grants, burst_at_us, sid, iuc):
    a = sdr_cmts.parser().parse_args(["--sim", "1"])
    mux, core, rx, tb, mcr, decim = sdr_cmts.build(a, log=lambda *x: None)
    events = []
    rx.on_burst = events.append
    rx.push(0, sdr_cmts.np.zeros(0, sdr_cmts.np.complex64))
    t0 = int(0.2 * TS_RATE) // mux.ms_counts * mux.ms_counts
    rx.add_grants([(t0 + int(s * US), t0 + int(e * US), i, g) for s, e, i, g in grants])
    cm = SimModem(MODEM, rx, delay_us=0, power_db=-20, freq_hz=250, snr_db=30, symrate=a.us_rate * 160e3,
                  ms_counts=mux.ms_counts)
    cm._send(t0 + burst_at_us * US, mac.rng_req_msg(MODEM, mux.sa, sid, 1), iuc, "RNG-REQ")
    n = int(rx.sample_of_ts(t0 + 0.01 * TS_RATE))
    rx.push(0, cm.render(0, n))
    rx.process()
    return [(e.iuc, float(e.err_counts / US)) for e in events if e.burst is not None and e.burst.frame]


B, IM, SM, REQ = mac.SID_BROADCAST, mac.IUC_INIT_MAINT, mac.IUC_STATION_MAINT, mac.IUC_REQUEST
CASES = [
    # name, grants (start µs, end µs, IUC, SID), burst start µs, RNG-REQ SID, expected (IUC, error µs)
    ("SM 2 ms après le début de l'IM (09/10)", [(0, 1200, IM, B), (2000, 2300, SM, 1)], 2000, 1, (SM, 0)),
    ("rafale tardive dans la marge IM, sans grant", [(0, 1200, IM, B)], 2000, 0, (IM, 2000)),
    ("Sagem non calé, 174 µs avant l'IM (dans un REQ)", [(-1000, 0, REQ, B), (0, 1200, IM, B)], -174, 0, (IM, -174)),
]
ok_all = True
for name, grants, at, sid, (want_iuc, want_us) in CASES:
    got = run(grants, at, sid, SM if sid else IM)
    ok = len(got) == 1 and got[0][0] == want_iuc and abs(got[0][1] - want_us) < 0.2
    ok_all &= ok
    print(f"{'OK ' if ok else 'KO '} {name:48s} -> {[(('IM', 'SM', 'REQ')[[IM, SM, REQ].index(i)], round(e, 2)) for i, e in got]}")
print("PASS" if ok_all else "FAIL")
sys.exit(0 if ok_all else 1)
