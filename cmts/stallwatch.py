"""TX stall watchdog: one dated line per gap in the B210 feed, saying where every thread was.

The main loop calls feed() after each block handed to UHD (one every ~2.5 ms). A watchdog thread
wakes every 5 ms; once the feed is late by more than `threshold`, it takes ONE snapshot of all
threads (Python stack top, kernel state R/S/D from /proc) while the stall is going on. When the
feed resumes, it prints:

  [TX] !! blocage de 468 ms à t=50.93 s : U +1, L +176 | chien de garde à l'heure | ...

How to read it:
  - watchdog on time and the main thread in UHD send()/recv: the program is waiting for the
    driver (USB, kernel) -> not a Python problem;
  - watchdog late by about the stall: the whole process (or the GIL) was held -> the thread
    that holds it shows in the snapshot taken as soon as the watchdog could run;
  - a thread in state D: uninterruptible wait in the kernel (page fault / memory compaction, I/O);
  - page faults: a jump means memory being mapped during the stall (huge pages, compaction).
U/L reported by UHD while the program kept feeding normally are printed as their own line.
"""
import collections
import gc
import os
import sys
import threading
import time


def _proc_faults():
    try:
        with open("/proc/self/stat") as f:
            v = f.read().rsplit(")", 1)[1].split()
        return int(v[7]), int(v[9])            # minflt, majflt
    except (OSError, IndexError, ValueError):
        return 0, 0


def _task_states():
    out = {}
    for tid in os.listdir("/proc/self/task"):
        try:
            with open(f"/proc/self/task/{tid}/stat") as f:
                s = f.read()
            out[int(tid)] = (s[s.index("(") + 1:s.rindex(")")], s.rsplit(")", 1)[1].split()[0])
        except (OSError, ValueError):
            pass
    return out


def _where(frame, depth=2):
    parts = []
    while frame is not None and len(parts) < depth:
        co = frame.f_code
        parts.append(f"{os.path.basename(co.co_filename)}:{frame.f_lineno} {co.co_name}")
        frame = frame.f_back
    return " < ".join(parts) or "?"


class StallWatch:
    def __init__(self, log, counters, docsis_time, threshold=0.025, period=0.005, l_ms=None):
        self.log, self.counters, self.docsis_time = log, counters, docsis_time
        self.l_ms = l_ms or (lambda n: None)
        self.threshold, self.period = threshold, period
        self.last = time.perf_counter()
        self.armed_at = 0.0                       # gaps before this (start-up) are not stalls
        self.done = collections.deque()           # (end, gap) of finished stalls, from feed()
        self.snap = None
        self.late_max = 0.0
        self.reported = dict(counters)
        self.gc_log = collections.deque(maxlen=64)  # (start, end, generation)
        self._gc_t0 = None
        gc.callbacks.append(self._gc)

    def _gc(self, phase, info):
        if phase == "start":
            self._gc_t0 = time.perf_counter()
        elif self._gc_t0 is not None:
            self.gc_log.append((self._gc_t0, time.perf_counter(), info.get("generation")))

    def _ms(self, n):
        ms = self.l_ms(n) if n else None
        return f" (~{ms:.0f} ms jetés)" if ms is not None else ""

    def feed(self):
        now = time.perf_counter()
        gap = now - self.last
        self.last = now
        if gap > self.threshold and now >= self.armed_at:
            self.done.append((now, gap))

    def _snapshot(self, now):
        frames = sys._current_frames()
        states = _task_states()
        threads = []
        for t in threading.enumerate():
            if t is threading.current_thread():
                continue
            st = states.get(t.native_id, ("", "?"))[1]
            threads.append(f"{t.name.split(' ')[0]} [{st}] {_where(frames.get(t.ident))}")
        py = {t.native_id for t in threading.enumerate()}
        others = [f"{n} [{s}]" for tid, (n, s) in states.items() if tid not in py]
        return dict(t=now, ts=self.docsis_time(), threads=threads, others=others, faults=_proc_faults())

    def _cpu(self):
        """{tid: ns on CPU} for every thread of the process (/proc schedstat: no GIL needed by
        the threads being measured, so it also shows who held the GIL during a stall)."""
        out = {}
        for tid in os.listdir("/proc/self/task"):
            try:
                with open(f"/proc/self/task/{tid}/schedstat") as f:
                    out[int(tid)] = int(f.read().split()[0])
                if int(tid) not in self.names:
                    with open(f"/proc/self/task/{tid}/comm") as f:
                        self.names[int(tid)] = f.read().strip()
            except (OSError, ValueError, IndexError):
                pass
        return out

    def run(self, stop):
        self.names = {}
        prev = time.perf_counter()
        cpu_ref, cpu_t = self._cpu(), prev
        last_stall = prev                     # last time a stall was seen going on or pending
        pending_since = None                  # U/L counters changed, not yet reported
        while not stop.is_set():
            time.sleep(self.period)
            now = time.perf_counter()
            late = now - prev - self.period
            prev = now
            if now - self.last > self.threshold and now >= self.armed_at:   # stall going on
                if self.snap is None:
                    self.snap = self._snapshot(now)
                self.late_max = max(self.late_max, late)
                last_stall = now
                continue
            if self.done:
                last_stall = now
                if now - self.done[0][0] > 0.15:          # let UHD report its U/L first
                    end, gap = self.done.popleft()
                    self._report(end, gap, cpu_ref, now - cpu_t)
                    cpu_ref, cpu_t = self._cpu(), now
                    pending_since = None
                continue
            if now - cpu_t > 0.02:
                cpu_ref, cpu_t = self._cpu(), now
            if any(self.counters[k] != self.reported[k] for k in "UL"):
                pending_since = pending_since or now
                if now - pending_since > 0.3 and last_stall < pending_since:
                    d = {k: self.counters[k] - self.reported[k] for k in "UL"}
                    self.reported.update(self.counters)
                    pending_since = None
                    self.log(f"[TX] !! U +{d['U']}, L +{d['L']}{self._ms(d['L'])} vers t={self.docsis_time():.2f} s sans blocage "
                             f"du programme (retard côté USB / pilote)")
            else:
                pending_since = None

    def _report(self, end, gap, cpu_ref, window):
        d = {k: self.counters[k] - self.reported[k] for k in "UL"}
        self.reported.update(self.counters)
        s, self.snap = self.snap, None
        late, self.late_max = self.late_max, 0.0
        start = end - gap
        gcs = [f"gen{g} {1e3 * (b - a):.0f} ms" for a, b, g in self.gc_log if b > start and a < end]
        cpu = self._cpu()
        used = sorted(((cpu[t] - cpu_ref[t]) / 1e6, self.names.get(t, str(t))) for t in cpu if t in cpu_ref)
        busy = ", ".join(f"{n} {ms:.0f}" for ms, n in reversed(used) if ms >= 5) or "aucun thread"
        cpu_line = f"CPU sur {window * 1e3:.0f} ms (ms par thread): {busy}"
        if s is None:
            self.log(f"[TX] !! blocage de {gap * 1e3:.0f} ms : U +{d['U']}, L +{d['L']}{self._ms(d['L'])} | chien de garde bloqué "
                     f"lui aussi (GIL tenu ou processus gelé) | GC: {', '.join(gcs) or 'aucun'}")
            self.log(f"   {cpu_line}")
            return
        mi, ma = _proc_faults()
        wd = (f"chien de garde en retard de {late * 1e3:.0f} ms (GIL ou CPU tenu)" if late > 0.02
              else "chien de garde à l'heure (attente hors Python)")
        self.log(f"[TX] !! blocage de {gap * 1e3:.0f} ms à t={s['ts']:.2f} s : U +{d['U']}, L +{d['L']}{self._ms(d['L'])} | {wd} | "
                 f"défauts de page +{mi - s['faults'][0]} (majeurs +{ma - s['faults'][1]}) | GC: {', '.join(gcs) or 'aucun'}")
        self.log(f"   {cpu_line}")
        for line in s["threads"]:
            self.log(f"   {line}")
        if s["others"]:
            self.log(f"   autres: {', '.join(s['others'])}")
