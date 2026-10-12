# mini-CMTS — a software EuroDOCSIS CMTS on one USRP B210

*[Version française : README.fr.md](README.fr.md)*

A Cable Modem Termination System written in Python (with C kernels) that drives a single
Ettus/NI **USRP B210** and brings a real, unmodified cable modem **online** in a closed lab:
EuroDOCSIS downstream, DOCSIS 1.x/2.0 upstream, ranging, DHCP/TFTP/ToD, registration
(DOCSIS 1.1 and 3.0), downstream channel bonding, DOCSIS 3.0 upstream channel bonding (Multiple
Transmit Channel mode) and an Ethernet bridge to the Linux host.

As far as we know there was no open-source CMTS able to do this with a low-cost SDR: this project
exists to learn DOCSIS hands-on and to let others do the same.

**Status: experimental lab tool.** Validated with one modem (Sagem F@st 3686, DOCSIS 3.0).

## Results (Sagem F@st 3686, 2026-10-12, 6-core i5-9500 Proxmox host)

| | Configuration | Measured (iperf3 through the modem) |
|---|---|---|
| Downstream | 6 bonded EuroDOCSIS 256-QAM channels (290-330 MHz, 306 primary in the middle) | **276-284 Mbit/s** (8-16 TCP flows; ~235 with 4 flows, 120-175 with 1 flow, latency-bound) |
| Upstream | 2 bonded ATDMA 64-QAM 5.12 Msym/s channels (60 + 53 MHz), `--us-channels 2 --us-target -20` | **51-52 Mbit/s** (8 flows, 45 min run); 22-24 with one channel |
| Latency | ping through the modem | ~75 ms idle, ~55 ms during a download (CoDel) |

Close to the physical limits of the channels used (6 × 256-QAM ≈ 300 Mbit/s; 2 × 64-QAM ATDMA
≈ 50 Mbit/s after FEC and burst overhead).

Also working (experimental, 2026-10-10, not the defaults):

| | Configuration | Status |
|---|---|---|
| Downstream | 3 bonded channels (306 + 314 + 322 MHz) | 142.8 Mbit/s measured once; the modem often leaves one channel out (partial service), see below |
| Upstream bonding | 2 channels ATDMA 64-QAM 5.12 Msym/s (60 + 53 MHz), `--us-channels 2` | modem online in Multiple Transmit Channel mode, ranged on both channels, frames reassembled from segments sent on both; throughput measured since: 44-47 Mbit/s, last row |
| Upstream bonding | 4 channels ATDMA 64-QAM 2.56 Msym/s (60 / 56.4 / 52.8 / 49.2 MHz), `--us-channels 4 --us-rate 16 --us-spacing -3.6e6 --us-target -20` | online, ping 30/30, 2640 segments from the 4 channels reassembled, none lost |
| Together | 3 downstream + 2 upstream channels | online every time; all 3 downstream channels acquired in 6 of 19 starts (TX gain 44-52 makes no clear difference), otherwise partial service on 2 |
| Together | 3 downstream + 4 upstream channels | online once with every channel, at the CPU limit of the 4-core laptop (upstream ~150 ms behind) |
| Together, radio process | `--radio-process --ds-channels 3` (3 DS + 1 US), 6-core PC (i5-9500) | **130.8 Mbit/s downstream / 20.2 Mbit/s upstream** (iperf3 through the modem); channels missed at registration are added back by CM-STATUS |
| Together, radio process | `--radio-process --ds-channels 3 --us-channels 2`, same PC | 130.8 / 15.8 Mbit/s: upstream bonding brings no throughput yet (upstream processes at 70-90 %, ~93 ms latency) |
| Downstream, radio process | `--radio-process --ds-channels 4`: master clock 34.76 MHz (5 samples/symbol), 6-core PC | **156 Mbit/s** downstream (131 in another run), 19.3 upstream; each of the 4 channels carries a quarter of the traffic; the 2 middle channels (either side of the TX LO) sometimes drop and come back through CM-STATUS |
| Downstream, radio process | `--radio-process --ds-channels 5`: master clock 55.616 MHz, AVX2 synthesis, 6-core PC | **215.6 Mbit/s** downstream, 20.2 upstream, ping 91 ms; 6 DS: 208-209 Mbit/s but the upstream collapses (2.9 Mbit/s) and ping up to 1 s, to be resumed |
| Downstream, radio process | `--radio-process --ds-channels 6 --ds-layout around`: 290-330 MHz, 306 primary in the middle, 55.616 MHz, 16 KB USB frames | **257-279 Mbit/s** downstream (channels 100 % full), 21 upstream, ping ~97 ms (170 ms during an upload); **the default** since 2026-10-11 |
| Together, radio process | 6 DS + `--us-channels 2`: upstream processes on every core at a lower priority than the transmit side, back-pressure on their delay, DDC in a thread of its own, one preamble search per burst | **269-277 Mbit/s downstream / 24 Mbit/s upstream**, modem never lost, 99.8 % of bursts decoded, ping 78 ms (~370 during an upload); TS packets built in C, frames spread over the channels in turn |
| Together, upstream bonding | 6 DS + `--us-channels 2` (defaults `--mtc-std --mtc-owed --ds-aqm 30`): multi-channel registration and requests per MULPI, as a real CMTS does them (Dynamic Range Window in the TCC, requests added up, grant pending), CoDel downstream, upstream segments decoded entirely in C | **270-284 Mbit/s downstream / 44-47 Mbit/s upstream** (iperf3, 8-16 flows), ping 75 ms; upstream bonding finally adds throughput |

## ⚠ Safety and legal

- **Closed coax only.** Never connect this to an operator's cable network: you would transmit
  on frequencies you have no licence for and disturb other customers.
- **Never connect the B210 RX input without attenuation**: the modem transmits up to +60 dBmV.
  See the wiring below (one 8 dB attenuator, the isolation of a 1:2 splitter).
- Only use your own modems. Order: start the program, then power the modem; to stop, power the
  modem off first, then Ctrl-C.

## Hardware

- USRP B210 on a **dedicated USB 3 controller**, Linux PC (tested: Arch Linux, UHD 4.9,
  Python 3.14, 4 cores / 8 threads laptop).
- One 1:2 splitter (coupler A), one 8 dB attenuator, a second 1:2 splitter for monitoring.

```
B210 TX/RX ── splitter A [in] ──┬── out 1 ── cable modem
                                └── out 2 ── 8 dB ── splitter B [in] ──┬── B210 RX2
                                                                        └── (optional receiver)
```

The modem's upstream reaches RX2 through the isolation between the outputs of splitter A.

## Quick start

```bash
# real-time priority for the shell that starts it (or set limits.d)
sudo prlimit --pid $$ --rtprio=99:99 --nice=40:40
python3 mini_cmts.py --all-in-one          # then power the modem on
```

`--all-in-one` also starts `provision.py` (DHCP 10.10.0.10-99, TFTP with a generated config file,
ToD) on the TAP interface `cmts0` (10.10.0.1/24). Without it, run your own DHCP/TFTP/ToD
servers on `cmts0`, like a classic DOCSIS install. Expected log: `RANGING TERMINÉ`, DHCP,
TFTP, `REG-ACK ok -> MODEM EN LIGNE` within about a minute. The console and logs are in French.

Defaults (since 2026-10-10) = the configuration of the results above:

| Option | Default | Meaning |
|---|---|---|
| `--freq` | 306 MHz | first downstream channel; bonded ones every `--ds-spacing` (8 MHz) |
| `--ds-channels` | 6 | 1 to 6 (master clock 27.808 / 34.76 / 55.616 MHz); 6: 234-258 / 21 Mbit/s on the 6-core PC |
| `--qam` | 256 | downstream QAM |
| `--us-freq`, `--us-rate`, `--us-mode`, `--us-mod` | 60 MHz, 32 (5.12 Msym/s), atdma, 64qam | upstream channel |
| `--us-target` | -14 dBFS | burst level the modems are ranged to |
| `--grant-ahead-hold/-every/-scale` | on, 3, 4 | unsolicited grants keeping the upload going |
| `--us-process` | on | upstream demodulation in its own process |
| `--radio-process` | on | B210, J.83 encoding, shaping and transmission in a process of their own (`cmts/radio.py`): without it, 3 downstream channels fall behind on a slower PC (GIL shared with the MAC layer) |
| `--us-channels`, `--us-spacing` | 1, -7 MHz | upstream channels (2: DOCSIS 3.0 upstream bonding, the second one 7 MHz below) |
| `--tx-queue` | 3 | blocks ready before the send (~1.9 ms each). With `--tx-frames 48`: downstream latency ~25 ms instead of 36, idle ping 100 -> 78 ms, smallest TX margin ~7 ms (long run still to do) |
| `--send-frame-size`, `--tx-frames` | 16360, 48 (14 ms of reserve) | 16 KB USB send frames (UHD: 8 KB) and how many in flight: half the USB submissions, send thread 23 -> 16 % of a core at 55.6 MS/s, 6 DS without late sends |
| `--ds-layout` | around | `--freq` (the primary) in the middle, the others on both sides (`up`: all above it); with 6 DS the only layout where the Sagem uses its upstream grants (234-247 / 21 Mbit/s) |
| `--recv-frame-size` | 16360 | 16 KB USB receive frames: the radio's RX thread 24 -> 14 % of a core at 27.8 MS/s |
| `--us-cpus` | auto | `own`: a core per upstream process; `shared`: every core, real-time priority 10 below the transmit side; `auto`: `shared` only when cores of their own would leave the transmit side of 5-6 DS fewer than 4 (6 DS + 2 US) |
| `--us-lag-limit` | 150 ms | back-pressure: an upstream channel whose receiver is this far behind gets no data grant (half of it: no grant in advance) until it catches up; 0 = off |
| `--mtc-std` | on | multi-channel upstream registration per MULPI, as a real CMTS (ARRIS) does it: Dynamic Range Window in the TCC (action 2, mandatory in MTC mode) and not in the RNG-RSPs, 47.3 criteria at 0, TLV 52 = 60; `--no-mtc-std`: the former registration, without DRW |
| `--mtc-owed` | on | multi-channel requests added up (the modem only requests new bytes), granted on every channel each MAP, Data Grant Pending while bytes are owed; `--no-mtc-owed`: each request replaces the previous one |
| `--ds-aqm` | 30 ms | CoDel on the downstream queues: a few frames dropped one at a time instead of bursts at the `--ds-queue-ms` limit; ping during a download 88 -> 55 ms; 0 = off |
| `--tx-dc-cal` | off | measure and cancel the B210's TX LO leakage at start (-2 to -24 dB; did not help the 3rd channel) |
| `--prov-args` | — | e.g. `"--us-burst 64000 --us-rate 1000000000"` (already the defaults; 1 Gbit/s both ways) |

`python3 mini_cmts.py --help` lists everything; `--sim SECONDS` runs a simulated modem without
hardware.

## How it works

- **Downstream** (`cmts/downstream.py`, `cmts/j83a.py`, C: `cmts/dsdsp.c`): MPEG-TS multiplexer
  carrying SYNC, UCD, MAP, MDD and data (data queues under CoDel), J.83 Annex A encoder (RS, interleaver, randomizer,
  256-QAM), RRC shaping; bonded channels shifted and summed in one C pass, sent to the B210 at
  4/6/8 samples per symbol. Every block is time-stamped: the downstream is the time base.
- **Upstream** (`cmts/upstream.py`, `cmts/us_phy.py`, C: `cmts/usdsp.c`, process: `cmts/usproc.py`):
  the RX stream is cut into the MAP intervals; per burst: preamble search, symbol interpolation,
  carrier, gain, equalizer cached per modem, symbol-clock offset compensation per modem,
  decision-directed tracking, descrambling, Reed-Solomon, MAC frames and concatenations. A segment
  burst of a modem whose equalizer is known (most multi-channel traffic) is decoded in one C call,
  equalizer refit in C when needed (`seg_fast`); the rest goes the Python way
  (`MINI_CMTS_US_FAST=0`: everything in Python).
- **MAC** (`cmts/ranging.py`, `cmts/registration.py`, `cmts/docsis_mac.py`): ranging
  (timing/power/frequency), bandwidth requests and grants (including grants in advance),
  REG-REQ(-MP)/REG-RSP(-MP)/REG-ACK with CMTS MIC, receive channel configuration and DSID for
  bonding, MDD for DOCSIS 3.0.
- **Upstream bonding** (`--us-channels 2`): the B210 receives one 27.808 MS/s stream centred
  between the upstream channels; one process per channel takes its channel out with a C DDC
  (shift, low-pass, decimation) and demodulates it. A DOCSIS 3.0 modem gets, in its REG-RSP-MP,
  Multiple Transmit Channel mode, a Transmit Channel Configuration (TLV 46: keep channel 1, add
  channel 2 with its UCD, the Dynamic Range Window), a SID cluster (TLV 47, 47.3 criteria) and
  TLV 52, as a real CMTS does (read off an ARRIS CMTS, receive only, `tools/sfr_maps.py`). It then sends its data as
  segments (8-byte segment header, sequence number) on both channels; the CMTS puts them back in
  order and cuts them into MAC frames again (Continuous Concatenation and Fragmentation). Requests
  are in bytes (queue-depth based requests) and only carry new bytes: the CMTS adds them up, grants
  them on every ranged channel and signals what it still owes with a Data Grant Pending.
- **Bridge** (`cmts/bridge.py`): Ethernet frames between the modems and the TAP interface.
- **Provisioning** (`provision.py`, `cmts/cmcfg.py`): DHCP, TFTP, ToD, config file generator.

## Lessons learned (the hard parts)

- Real-time on a PC: real-time priority, keep the B210's USB interrupt core free, move other
  interrupts away (a touchpad interrupt was enough to break the downstream), never write files
  from the transmit thread.
- The modem's **Max Traffic Burst** (config file TLV 24.9) caps each bandwidth request: the default
  3044 bytes limited the upload to ~3.5 Mbit/s.
- The Sagem puts a zero piggyback request in almost every frame: grants in advance must not stop on it.
- The modem's upstream symbol clock was 4.3 ppm off: harmless on short bursts, fatal on 3 ms ones.
- 16-QAM TDMA preambles are 16-QAM too (the label/point map had to be found by brute force).
- Upstream bonding with the Sagem: an upstream channel above 65 MHz (the top of the EuroDOCSIS
  upstream band) gets REG-ACK 209 (reject-bad-tcc), with no further detail. The Dynamic Range
  Window TCC, mandatory in MTC mode, got 209 too as long as it was sent as "no action" after the
  RNG-RSPs' TLV 14. It is accepted in the form a real CMTS uses (action "change", UCID 0, nothing
  in the RNG-RSPs): listening to one, receive only, showed it. The modem pads every grant with a
  segment of stuff bytes: those must not keep the grants in advance going.
- The second upstream channel added nothing (22-24 Mbit/s with 1 or 2 channels):
  - a multi-channel request only carries new bytes, and the CMTS replaced them instead of adding
    them up;
  - the upstream receivers, half Python, saturated the CPU.

  Requests added up with grant pending, then segment bursts decoded in C: 44-47 Mbit/s.
- UHD calibrates the AD9361 TX quadrature when the streams open, with a test tone looped back
  through the RX analog filter; the tone grows with the master clock. With the RX filter at the
  upstream width (12.4 MHz) the calibration failed from 41.712 MHz on: the TX image (mirror of
  the signal around the LO) stayed at -21..-24 dBc instead of -51, and the modem never locked
  above 34.76 MHz. The streams now open with a 56 MHz RX filter, narrowed afterwards
  (`radio.TX_CAL_RX_BW`): the modem locks at 41.712 and 55.616 MHz. 5 channels at 55.616 MS/s
  are then beyond the 6-core Proxmox host's CPU.
- With 3 channels the middle one sits on the TX LO: its leakage was ~-31 dBc in that channel.

Details, measurements and every bug found: `docs/JOURNAL.fr.md` (French) and `docs/TESTS.fr.md`.

## Tests

```bash
for t in tests/test_*.py; do python3 $t | tail -1; done   # test_provision_tap.py needs sudo
```

Most tests replay real bursts of the modem (`tests/data/sagem3686/`).

## Roadmap

- Upstream bonding: long stability run, less CPU in the upstream receiver (block phase tracking,
  SIMD Reed-Solomon), then more than 2 channels.
- 3 downstream channels reliably (the modem often leaves one out), then 4+: blocked by the
  27.808 MHz master clock constraint above, i.e. a second B210 sharing the same 10 MHz / PPS.
- Spreading the CMTS over several SDRs / PCs.

## Licence

To be chosen by the project owner before publication.

Specifications referred to (not included): CableLabs DOCSIS MULPIv3.0 and PHYv3.0, ITU-T J.83,
ETSI EN 300 429.
