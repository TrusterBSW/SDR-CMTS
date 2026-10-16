#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""Drift between the clocks of several B210s (no common 10 MHz): every device's time is read
against the host's monotonic clock; a straight line per device gives its rate against the host,
the differences between devices their relative drift, i.e. how fast the downstream channels of a
follower radio slide against the primary's (sdr_cmts --ds-radios 2).

  tools/clock_drift.py SERIAL1,SERIAL2 [--seconds 600] [--mcr 27.808e6] [--every 0.5]

Each reading brackets usrp.get_time_now() between two host clock reads and keeps the reading
with the shortest round trip of every interval (USB control latency: ~0.1-1 ms, its jitter is
the measurement noise). The host clock cancels out of the relative drift."""
import argparse
import time

import numpy as np


def read(usrp, n=8):
    """(host time, device time) of the reading with the shortest round trip out of n."""
    best = None
    for _ in range(n):
        h0 = time.perf_counter()
        d = usrp.get_time_now().get_real_secs()
        h1 = time.perf_counter()
        if best is None or h1 - h0 < best[0]:
            best = (h1 - h0, (h0 + h1) / 2, d)
    return best[1], best[2], best[0]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("serials")
    ap.add_argument("--seconds", type=float, default=600.0)
    ap.add_argument("--every", type=float, default=0.5)
    ap.add_argument("--mcr", type=float, default=27.808e6)
    ap.add_argument("--csv", help="every reading (serial, host s, device s, round trip s) into this file")
    a = ap.parse_args()
    import uhd
    serials = [s.strip() for s in a.serials.split(",") if s.strip()]
    devs = []
    for s in serials:
        u = uhd.usrp.MultiUSRP(f"type=b200,serial={s},master_clock_rate={a.mcr}")
        u.set_clock_source("internal")
        u.set_time_now(uhd.types.TimeSpec(0.0))
        devs.append(u)
    time.sleep(1.0)        # (right after set_time_now a B210 may still return its former time once)
    t_end = time.perf_counter() + a.seconds
    rows = {s: [] for s in serials}
    t_print = time.perf_counter() + 30
    while time.perf_counter() < t_end:
        for s, u in zip(serials, devs):
            rows[s].append(read(u))
        if time.perf_counter() > t_print:
            t_print += 30
            print(report(rows, short=True), flush=True)
        time.sleep(a.every)
    print(report(rows, short=False))
    if a.csv:
        with open(a.csv, "w") as f:
            for s, r in rows.items():
                for h, d, rtt in r:
                    f.write(f"{s},{h:.9f},{d:.9f},{rtt:.9f}\n")


def fit(r):
    h = np.array([x[0] for x in r])
    d = np.array([x[1] for x in r])
    k, c = np.polyfit(h - h[0], d, 1)
    res = d - (k * (h - h[0]) + c)
    return k, c, h, d, res


def report(rows, short):
    out = []
    fits = {}
    for s, r in rows.items():
        if len(r) < 4:
            return "…"
        k, c, h, d, res = fit(r)
        fits[s] = (k, c, h)
        rtt = np.median([x[2] for x in r]) * 1e6
        out.append(f"{s}: {(k - 1) * 1e6:+8.3f} ppm contre l'hôte, résidu {np.std(res) * 1e6:5.1f} µs, "
                   f"aller-retour médian {rtt:.0f} µs, {len(r)} lectures sur {h[-1] - h[0]:.0f} s")
    ss = list(rows)
    for s in ss[1:]:
        rel = (fits[s][0] - fits[ss[0]][0]) / fits[ss[0]][0]
        span = fits[s][2][-1] - fits[s][2][0]
        out.append(f"{s} contre {ss[0]} : {rel * 1e6:+.3f} ppm -> {abs(rel) * 3600 * 1e3:.1f} ms par heure, "
                   f"{abs(rel) * span * 1e6:.0f} µs sur cette mesure ; fenêtre de 10 ms atteinte en "
                   f"{(0.010 / abs(rel) / 60) if rel else float('inf'):.0f} min")
    return "\n".join(out) if not short else " | ".join(out)


if __name__ == "__main__":
    main()
