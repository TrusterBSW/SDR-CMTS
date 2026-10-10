"""CMTS ranging (MULPIv3.0 7.1.3 and 10.2.3.7): RNG-REQ -> RNG-RSP, temporary SIDs and
Station Maintenance polling.

    broadcast Initial Maintenance:  RNG-REQ (SID 0)  -> RNG-RSP (temporary SID, continue)
    Station Maintenance (our SID):  RNG-REQ (SID)    -> RNG-RSP (continue ... success)
    then a Station Maintenance opportunity every --sm-period (T4 = 30 s on the modem side)

Thread use: on_burst() runs in the upstream worker thread, schedule() in the downstream
encoder thread (it is the MAP scheduler hook of DownstreamMux).
"""
import collections
import threading
import time

from . import cmcfg, registration
from . import docsis_mac as mac
from .upstream import TS_RATE

SM_MINISLOTS = 12          # Station Maintenance grant (RNG-REQ burst = 9 mini-slots at 1.28 Msym/s)
MAX_MISSES = 16


class Modem:
    def __init__(self, addr, sid):
        self.mac, self.sid = addr, sid
        self.state = "ranging"          # ranging -> ranged -> registering -> online
        self.reg = None                 # (REG-REQ body, REG-RSP frame) of the last registration
        self.flows = []
        self.next_sm = None             # 10.24 MHz counts; None = no poll scheduled
        self.poll_deadline = None       # a poll is outstanding until its answer is handled
        self.misses = 0
        self.n_req = 0
        self.last = {}
        self.success_at = None


def fmt_mac(b):
    return ":".join(f"{x:02x}" for x in b)


class CmtsCore:
    def __init__(self, mux, us_chan_id=1, target_db=-20.0, sm_period=1.0, sm_retry=0.03,
                 respond=True, tol_counts=1.5, tol_db=1.5, tol_hz=300.0, log=print,
                 secret="docsis-lab", strict_mic=False, on_data=None, grant_ahead_s=0.1, grant_ahead_hold=False,
                 grant_ahead_every=1, grant_ahead_scale=1.0):
        self.mux = mux
        self.us_chan_id = us_chan_id
        self.target = target_db
        self.sm_period = int(sm_period * TS_RATE)
        self.sm_retry = int(sm_retry * TS_RATE)
        self.respond = respond
        self.tol = (tol_counts, tol_db, tol_hz)
        self.log = log
        self.lock = threading.Lock()
        self.modems = {}      # MAC -> Modem
        self.by_sid = {}
        self.next_sid = 1
        self.events = collections.deque(maxlen=20000)   # (wall time, kind, dict): tests, summaries
        self.n_rsp = 0        # RNG-RSP sent
        self.req_log_t = 0.0
        self.req_unlogged = 0
        self.pending = {}     # SID -> mini-slots requested, granted in the next MAP
        self.secret, self.strict_mic = secret, strict_mic
        self.on_data = on_data            # on_data(ethernet frame, fcs_ok, sid): bridge hook
        self.next_sfid = 1
        self.grants = 0
        # Grants in advance: a SID that is sending also gets, in every MAP, a grant the size of
        # its last request, until grant_ahead_s after its last request or data burst. (Sized on
        # the largest recent request, the grants and the windows to search grew needlessly.)
        # Without it, each frame waits for its own request -> MAP round trip (~55 ms on this
        # bench: 18 frames/s). A piggyback request of 0 or an empty grant does NOT stop them:
        # the Sagem 3686 puts a 0 piggyback in nearly every frame (~100/s), its queue being empty
        # only until the next TCP ACKs arrive; stopping there cut a fast.com upload to 1.2 Mbit/s
        # (2026-10-09). The window simply runs out once the SID stops sending. Timed on the
        # DOCSIS clock (MAP start times), so it is the same in simulation and on the bench. 0 = off.
        self.grant_ahead_s = grant_ahead_s
        # False: the former rule, the grants in advance stop on a 0 request or an empty grant
        # (light on the upstream receiver; the upload stays ~1.2 Mbit/s). True: the rule above.
        self.grant_ahead_hold = grant_ahead_hold
        # one grant in advance every `every` MAPs, `scale` times the last request (max 255
        # mini-slots): as much air time, `every` times fewer bursts to demodulate. The upstream
        # receiver could not keep up with ~490 bursts/s (2026-10-09).
        self.grant_ahead_every = max(1, int(grant_ahead_every))
        self.grant_ahead_scale = grant_ahead_scale
        self._maps = 0
        self.active = {}      # SID -> (DOCSIS time the advance grants stop, mini-slots)
        self.n = dict(req=0, ahead=0, empty=0, concat=0)   # grants asked, given in advance, unused
        self.bonding = None   # [(DCID, frequency)] when several downstream channels are bonded
        self.next_dsid = 0x1001
        self.bonded = {}      # modem MAC -> DSID of its bonded unicast traffic, once online
        self.bonded_channels = {}   # modem MAC -> DCIDs it acquired (REG-ACK partial service)

    # ------------------------------------------------------------ MAP hook (downstream thread)
    def channel_scheduler(self, ucid):
        """MAP scheduler of a further upstream channel (DownstreamMux.add_upstream)."""
        return lambda start_ms, free_ms: self.schedule_channel(ucid, start_ms, free_ms)

    def schedule_channel(self, ucid, start_ms, free_ms):
        return []

    def schedule(self, start_ms, free_ms):
        start_ts = start_ms * self.mux.ms_counts
        out = []
        with self.lock:
            for m in self.modems.values():
                due = m.next_sm is not None and m.next_sm <= start_ts
                stuck = m.poll_deadline is not None and start_ts > m.poll_deadline
                if (due or stuck) and len(out) * SM_MINISLOTS + SM_MINISLOTS <= free_ms:
                    out.append((m.sid, mac.IUC_STATION_MAINT, SM_MINISLOTS))
                    # one poll in flight: the next one is planned once this one is answered
                    # (RNG-RSP sent) or reported empty, never earlier, so that a correction
                    # is never measured twice before the modem has applied it
                    m.next_sm = None
                    m.poll_deadline = start_ts + int(TS_RATE)
            used = sum(n for _, _, n in out)
            iuc_s, iuc_l = mac.data_iucs()
            short = mac.BURST_PROFILES[iuc_s]["max_burst"]
            granted = set()
            for sid, n in list(self.pending.items()):
                if used + n > free_ms:
                    continue
                out.append((sid, iuc_s if n <= short else iuc_l, n))
                used += n
                del self.pending[sid]
                granted.add(sid)
                self.grants += 1
                self.n["req"] += 1
            self._maps += 1
            give = self._maps % self.grant_ahead_every == 0
            for sid, (until, n) in list(self.active.items()):
                if start_ts > until or sid not in self.by_sid:
                    del self.active[sid]
                    continue
                n = min(255, max(n, int(round(n * self.grant_ahead_scale))))
                if give and sid not in granted and used + n <= free_ms:
                    out.append((sid, iuc_s if n <= short else iuc_l, n))
                    used += n
                    self.n["ahead"] += 1
        return out

    # ------------------------------------------------------------ bursts (upstream thread)
    def on_burst(self, ev):
        b = ev.burst
        ch = getattr(ev, "us_ch", 1)
        if ch != 1:
            if b is not None and b.frame is not None:
                self.log(f"[US{ch}] trame reçue : {b.info}")
            return
        if b is None:                                   # nothing in a unicast grant
            if ev.iuc == mac.IUC_STATION_MAINT:
                self._miss(ev.sid)
            elif ev.iuc in (mac.IUC_SHORT_DATA, mac.IUC_LONG_DATA, mac.IUC_ADV_SHORT, mac.IUC_ADV_LONG):
                self.n["empty"] += 1                  # hold: the window runs out by itself
                if not self.grant_ahead_hold:
                    with self.lock:
                        self.active.pop(ev.sid, None)
            return
        info = b.info or {}
        where = {mac.IUC_INIT_MAINT: "IM", mac.IUC_STATION_MAINT: "SM", mac.IUC_REQUEST: "REQ"}.get(ev.iuc, ev.iuc)
        if b.frame is None:
            self.log(f"[US] rafale {where} non décodée ({b.error}): {b.power_db:.1f} dBFS, MER {b.mer:.1f} dB, "
                     f"{ev.err_counts / TS_RATE * 1e6:+.2f} µs, {b.freq:+.0f} Hz, corr {b.rho:.2f}"
                     + (f"\n     début du 1er bloc RS : {b.head}" if b.head else ""))
            if ev.iuc == mac.IUC_STATION_MAINT:
                self._miss(ev.sid)
            return
        if ev.iuc in mac.data_iucs() and self.grant_ahead_s and self.grant_ahead_hold:
            self._extend_ahead(ev.sid)                  # still sending: keep the grants coming
        if info.get("fc") == mac.FC_CONCAT:             # several frames in one burst
            subs = mac.split_concat(b.frame, info)
            self.n["concat"] += 1
            if len(subs) != info.get("parm", len(subs)):
                self.log(f"[US] !! concaténation SID {ev.sid:#06x}: {len(subs)} trame(s) lisible(s) sur {info.get('parm')}")
            for frame, sub in subs:
                self._dispatch(ev, b, frame, sub, where)
            return
        self._dispatch(ev, b, b.frame, info, where)

    def _dispatch(self, ev, b, frame, info, where):
        """One MAC frame (alone in its burst, or out of a concatenation)."""
        kind = info.get("kind")
        mtype = info.get("mtype")
        if kind == "MGMT" and mtype in (mac.RNG_REQ, mac.INIT_RNG_REQ, mac.B_INIT_RNG_REQ) \
                and info.get("crc_ok"):
            self._ranging(ev, b, info, where)
        elif kind == "REQ":
            self._request(info["sid"], info["minislots"], b)
        elif kind == "DATA":
            pig = mac.piggyback_request(info)
            if pig:
                self._request(*pig, b, piggyback=True)
            eth, ok = mac.pdu_ethernet(frame, info)
            if self.on_data:
                self.on_data(eth, ok, ev.sid)
        elif kind == "MGMT" and mtype in (mac.REG_REQ, mac.REG_ACK, mac.REG_REQ_MP) and info.get("crc_ok"):
            self._registration(info, b)
        else:
            self.log(f"[US] trame {where} reçue: {info}")

    def _extend_ahead(self, sid):
        """A data burst from `sid`: its grants in advance go on for grant_ahead_s from now."""
        with self.lock:
            if sid in self.active:
                self.active[sid] = (self.mux.now_ts + int(self.grant_ahead_s * TS_RATE), self.active[sid][1])

    def _request(self, sid, minislots, b, piggyback=False):
        """Bandwidth request (REQ frame or piggyback): granted in the next MAP."""
        self.events.append((time.time(), "REQ", dict(sid=sid, minislots=minislots)))
        with self.lock:
            known = sid in self.by_sid
            if known and minislots:
                self.pending[sid] = minislots
                if self.grant_ahead_s:
                    self.active[sid] = (self.mux.now_ts + int(self.grant_ahead_s * TS_RATE), minislots)
            elif known and not self.grant_ahead_hold:   # 0: nothing more queued (former rule)
                self.active.pop(sid, None)
            # hold: a 0 (piggyback) only means nothing more right now; the grants go on
        now = time.time()
        if now - self.req_log_t > 1:
            more = f" (+{self.req_unlogged} autres en 1 s)" if self.req_unlogged else ""
            self.req_log_t, self.req_unlogged = now, 0
            what = "grant au MAP suivant" if known else "SID inconnu, ignorée"
            self.log(f"[US] REQ{' piggyback' if piggyback else ''} SID {sid:#06x}: {minislots} mini-slots "
                     f"({b.power_db:.1f} dBFS) -> {what}{more}")
        else:
            self.req_unlogged += 1

    def _registration(self, info, b):
        sid, body = info["sid"], info["body"]
        with self.lock:
            m = self.by_sid.get(sid)
        if m is None:
            self.log(f"[REG] !! {('REG-REQ', 'REG-ACK')[info['mtype'] == mac.REG_ACK]} de SID {sid:#06x} inconnu")
            return
        if info["mtype"] == mac.REG_ACK:
            resp = body[2] if len(body) > 2 else None
            lost = []
            if resp == 30:                              # partial service: registered, fewer channels
                for t, v in cmcfg.parse(body[3:]):
                    if t == 49:
                        lost += [x for s_, sv in cmcfg.parse(v) if s_ == 6 for x in sv]
                self.log(f"[REG] !! {fmt_mac(m.mac)} REG-ACK : service partiel, canaux descendants non acquis : "
                         f"{lost or '?'}")
                if self.bonding and getattr(m, "dsid", None):
                    kept = [c for c, _ in self.bonding if c not in lost]
                    m.channels_ok = kept                 # the bridge only uses these
                    if len(kept) < 2:
                        m.dsid = None                    # only the primary left: no bonding
                resp = 0
            if resp == 0:
                if m.state != "online":
                    m.state, m.online_at = "online", time.time()
                    self.log(f"[REG] {fmt_mac(m.mac)} SID {sid:#06x}: REG-ACK ok -> MODEM EN LIGNE")
                    if getattr(m, "dsid", None):
                        self.bonded[m.mac] = m.dsid
                        ok = getattr(m, "channels_ok", None) or [c for c, _ in self.bonding]
                        self.bonded_channels[m.mac] = ok
                        self.log(f"[REG] {fmt_mac(m.mac)} : trafic unicast agrégé sur "
                                 f"{len(ok)} canaux ({', '.join(map(str, ok))}), DSID {m.dsid:#x}")
            else:
                self.log(f"[REG] !! {fmt_mac(m.mac)} REG-ACK réponse {resp}: " + "; ".join(
                    cmcfg.describe(cmcfg.parse(body[3:]), indent="")))
            return
        mp = info["mtype"] == mac.REG_REQ_MP          # DOCSIS 3.0 (MDD present): SID, fragments, TLVs
        name = "REG-REQ-MP" if mp else "REG-REQ"
        if mp:
            nfrag, fseq = body[2], body[3]
            m.frags = getattr(m, "frags", {})
            m.frags[fseq] = body[4:]
            if len(m.frags) < nfrag:
                return                                  # wait for the other fragments
            raw_tlvs = b"".join(m.frags[k] for k in sorted(m.frags))
            m.frags = {}
            body = body[:4] + raw_tlvs                  # same key for the retransmission check
        else:
            raw_tlvs = body[2:]
        try:
            tlvs = cmcfg.parse(raw_tlvs)
        except ValueError as e:
            self.log(f"[REG] !! {name} de {fmt_mac(m.mac)} illisible: {e}")
            return
        if m.reg and m.reg[0] == body:                  # retransmission: same answer
            self.mux.ucast.append((m.reg[1], None))
            self.log(f"[REG] {fmt_mac(m.mac)} {name} répété -> même réponse")
            return
        self.log(f"[REG] {fmt_mac(m.mac)} SID {sid:#06x}: {name} ({len(body)} octets, {b.power_db:.1f} dBFS)")
        for line in cmcfg.describe(tlvs):
            self.log(line)
        got = dict(tlvs).get(cmcfg.CMTS_MIC)
        mic_ok = got is not None and cmcfg.cmts_mic(tlvs, self.secret) == got
        response = 0
        if not mic_ok:
            self.log(f"[REG] !! CMTS MIC {'absent' if got is None else 'faux'} (secret « {self.secret} » "
                     f"différent de celui du fichier de config ?)"
                     + (" -> refus" if self.strict_mic else " -> accepté quand même (--strict-mic pour refuser)"))
            if self.strict_mic:
                response = 1

        def new_sfid():
            self.next_sfid += 1
            return self.next_sfid - 1

        def new_sid():
            with self.lock:
                s = self.next_sid
                self.next_sid = self.next_sid % 0x1FFF + 1
                self.by_sid[s] = m
            return s

        bonded = bool(mp and self.bonding)
        rsp_tlvs, flows = registration.build_response(tlvs, m.sid, new_sfid, new_sid,
                                                      version=3 if mp else getattr(self, "docsis_version", 1),
                                                      bonded=bonded) if response == 0 else (b"", [])
        if bonded and response == 0:
            rcc, rcp_id = registration.receive_channel_config(tlvs, self.bonding)
            rsp_tlvs += rcc
            if rcp_id:
                m.dsid = getattr(m, "dsid", None) or self.next_dsid
                self.next_dsid += 1
                rsp_tlvs += registration.dsid_encoding(m.dsid, [cid for cid, _ in self.bonding])
            self.log(f"[REG] RCC : profil {rcp_id.hex() if rcp_id else 'aucun adapté'}, canaux "
                     + ", ".join(f"{cid}@{f / 1e6:.0f} MHz" for cid, f in self.bonding))
        frame = (mac.reg_rsp_mp_msg if mp else mac.reg_rsp_msg)(self.mux.sa, m.mac, sid, response, rsp_tlvs)
        m.reg, m.flows = (body, frame), flows
        if response == 0:
            m.state = "registering"
        self.mux.ucast.append((frame, None))
        self.log(f"[REG] {'REG-RSP-MP' if mp else 'REG-RSP'} {registration.RESPONSE.get(response, response)} -> {fmt_mac(m.mac)}: "
                 + ", ".join(f"flux {d} réf {r} SFID {f}" + (f" SID {s:#06x}" if s else "") for d, r, f, s in flows))

    def _miss(self, sid):
        with self.lock:
            m = self.by_sid.get(sid)
            if not m:
                return
            m.misses += 1
            m.poll_deadline = None
            m.next_sm = self.mux.now_ts + self.sm_retry
            if m.misses >= MAX_MISSES:
                self.log(f"[CMTS] {fmt_mac(m.mac)} SID {sid:#06x}: {MAX_MISSES} Station Maintenance sans "
                         f"réponse, modem oublié")
                for s in [s for s, x in self.by_sid.items() if x is m]:
                    del self.by_sid[s]
                    self.pending.pop(s, None)
                del self.modems[m.mac]

    def _ranging(self, ev, b, info, where):
        addr = info["sa"]
        req_sid = info.get("sid", 0)
        if ev.iuc == mac.IUC_INIT_MAINT and req_sid and req_sid in self.by_sid:
            # a Station Maintenance answer measured against the IM interval: its timing error
            # is meaningless (a +2000 us "correction" lost the modem on 2026-10-09)
            self.log(f"[US] !! RNG-REQ de {fmt_mac(addr)} SID {req_sid:#06x} trouvée dans une fenêtre IM: "
                     f"réponse de Station Maintenance mal attribuée, ignorée")
            return
        initial = ev.iuc == mac.IUC_INIT_MAINT or req_sid == 0
        with self.lock:
            m = self.modems.get(addr)
            if m is None:
                if not initial:
                    self.log(f"[US] RNG-REQ de {fmt_mac(addr)} en {where} mais modem inconnu: ignoré")
                    return
                m = Modem(addr, self.next_sid)
                self.next_sid = self.next_sid % 0x1FFF + 1
                self.modems[addr], self.by_sid[m.sid] = m, m
            elif not initial and ev.sid != m.sid:
                self.log(f"[US] RNG-REQ de {fmt_mac(addr)} dans le SM du SID {ev.sid:#x}: ignoré")
                return
            if initial:                                 # (re)initialisation: forget the registration
                m.state, m.reg, m.flows = "ranging", None, []
                self.bonded.pop(m.mac, None)
            m.misses = 0
            m.n_req += 1
            # measurements -> adjustments
            ta = round(ev.err_counts)                              # >0: the CM must transmit earlier
            dp = self.target - b.power_db
            pq = max(-128, min(127, round(4 * dp)))
            fq = round(b.freq if b.inverted else -b.freq)
            fq = max(-32768, min(32767, fq))
            ok = (not initial and abs(ev.err_counts) <= self.tol[0] and abs(dp) <= self.tol[1]
                  and abs(b.freq) <= self.tol[2])
            status = mac.RNG_SUCCESS if ok else mac.RNG_CONTINUE
            newly = ok and m.state == "ranging"
            if newly:
                m.state, m.success_at = "ranged", time.time()
            m.last = dict(err_us=ev.err_counts / TS_RATE * 1e6, power=b.power_db, freq=b.freq, mer=b.mer)
            rx_ts = ev.rx_ts
        line = (f"[US] RNG-REQ {fmt_mac(addr)} {where} SID {info.get('sid', 0):#06x} -> {m.sid:#06x}: "
                f"arrivée {ev.err_counts / TS_RATE * 1e6:+8.3f} µs, {b.power_db:6.1f} dBFS, "
                f"{b.freq:+6.0f} Hz, MER {b.mer:4.1f} dB"
                + (f", RS corr {b.rs_fixed}" if b.rs_fixed else "") + (", octets LSB-first" if b.bitrev else "")
                + (", spectre inversé" if b.inverted else "")
                + ("  !! SATURATION ADC: baisse --us-gain" if b.peak > 0.95 else ""))
        self.events.append((time.time(), "RNG-REQ", dict(sid=m.sid, initial=initial, **m.last)))
        if not self.respond:
            self.log(line + "  (écoute seule, pas de RNG-RSP)")
            return
        frame = mac.rng_rsp_msg(self.mux.sa, addr, m.sid, self.us_chan_id, ta, pq, fq, status)
        sname = "success" if status == mac.RNG_SUCCESS else "continue"

        def sent(air_ts, m=m, ok=ok, rx_ts=rx_ts):
            delay_ms = (air_ts - rx_ts) / TS_RATE * 1e3
            with self.lock:
                m.next_sm = air_ts + (self.sm_period if ok else self.sm_retry)
                m.poll_deadline = None
            self.events.append((time.time(), "RNG-RSP", dict(sid=m.sid, status=sname, delay_ms=delay_ms)))
            self.n_rsp += 1
            self.log(f"     RNG-RSP {sname:8s} TA {ta:+d} ({ta / TS_RATE * 1e6:+.2f} µs) P {pq / 4:+.2f} dB "
                     f"F {fq:+d} Hz, à l'antenne {delay_ms:.0f} ms après la rafale"
                     + ("  !! > 200 ms (T3 du modem)" if delay_ms > 200 else ""))
        self.log(line)
        self.mux.ucast.append((frame, sent))
        if newly:
            self.log(f"[CMTS] {fmt_mac(addr)} SID {m.sid:#06x}: RANGING TERMINÉ (success)")

    def acquiring(self):
        """True when no modem is known or one is not online yet (Initial Maintenance needed)."""
        with self.lock:
            return not self.modems or any(m.state != "online" for m in self.modems.values())

    def summary(self):
        with self.lock:
            return [dict(mac=fmt_mac(m.mac), sid=m.sid, state=m.state, n_req=m.n_req, **m.last)
                    for m in self.modems.values()]
