# mini-CMTS — a software EuroDOCSIS CMTS on USRP B210s (or a bladeRF)

*[Version française : README.fr.md](README.fr.md)*

A Cable Modem Termination System written in Python (with C kernels) that drives one or more
Ettus/NI **USRP B210** (or a Nuand **bladeRF 2.0 micro**) and brings a real, unmodified cable modem **online** in a closed lab:
EuroDOCSIS downstream, DOCSIS 1.x/2.0 upstream, ranging, DHCP/TFTP/ToD, registration
(DOCSIS 1.1 and 3.0), downstream channel bonding, DOCSIS 3.0 upstream channel bonding (Multiple
Transmit Channel mode) and an Ethernet bridge to the Linux host. Installs as a Debian package,
runs as a systemd service, with a status page in the browser.

As far as we know there was no open-source CMTS able to do this with a low-cost SDR: this project
exists to learn DOCSIS hands-on and to let others do the same.

**Status: experimental lab tool.** Validated with one modem: a Sagem F@st 3686 (DOCSIS 3.0) running
the SFR operator firmware (FAST3686_SFR-PC20_3.649.0); every measurement below was made with it.

## Results (Sagem F@st 3686 modem; one B210: 6-core i5-9500 host, 2026-10-12)

| | Configuration | Measured (iperf3 through the modem) |
|---|---|---|
| Downstream | 6 bonded EuroDOCSIS 256-QAM channels (290-330 MHz, 306 primary in the middle) | **276-284 Mbit/s** (8-16 TCP flows; ~235 with 4 flows, 120-175 with 1 flow, latency-bound) |
| Upstream | 2 bonded ATDMA 64-QAM 5.12 Msym/s channels (60 + 53 MHz), `--us-channels 2 --us-target -20` | **51-52 Mbit/s** (8 flows, 45 min run); 22-24 with one channel |
| Latency | ping through the modem | ~75 ms idle, ~55 ms during a download (CoDel) |
| Two B210s | 2 + 2 DS, 2 US, no common 10 MHz (Threadripper 2950X, `configs/2xb210.toml`) | **188-189 / 49.5 Mbit/s**, 86 min run with the follower realigned (12.4 ms in total) |
| bladeRF 2.0 micro | 3 DS + 1 US (`configs/bladerf.toml`) | **138 / 24.5 Mbit/s**, see `docs/BLADERF.md` |

Close to the physical limits of the channels used (6 × 256-QAM ≈ 300 Mbit/s; 2 × 64-QAM ATDMA
≈ 50 Mbit/s after FEC and burst overhead).

Earlier steps (1 to 5 channels, single-process, upstream bonding before it added throughput):
[docs/HISTORY.md](docs/HISTORY.md).

## ⚠ Safety and legal

- **Closed coax only.** Never connect this to an operator's cable network: you would transmit
  on frequencies you have no licence for and disturb other customers.
- **Never connect the B210 RX input without attenuation**: the modem transmits up to +60 dBmV.
  See the wiring below (one 8 dB attenuator, the isolation of a 1:2 splitter).
- Only use your own modems. Order: start the program, then power the modem; to stop, power the
  modem off first, then Ctrl-C.

## Hardware

- USRP B210 on a **dedicated USB 3 controller**, Linux PC. Tested: Arch Linux (UHD 4.9,
  Python 3.14, 4-core laptop), Debian 13 (UHD 4.8, Python 3.13, 6-core i5-9500), Debian 12
  (UHD 4.3, Python 3.11, 16-core Threadripper 2950X with two B210s).
- Or a **bladeRF 2.0 micro** (`--sdr bladerf`, 3 DS + 1 US): see `docs/BLADERF.md`.
- One 1:2 splitter (coupler A), one 8 dB attenuator, a second 1:2 splitter for monitoring.

```
B210 TX/RX ── splitter A [in] ──┬── out 1 ── cable modem
                                └── out 2 ── 8 dB ── splitter B [in] ──┬── B210 RX2
                                                                        └── (optional receiver)
```

The modem's upstream reaches RX2 through the isolation between the outputs of splitter A.

Two B210s: their TX/RX ports on a 1:2 coupler, their RX2 ports on another one (behind the 8 dB
attenuator), and these two couplers and the modem on the **outputs** of a 1:3 coupler.

## Install and start

Full guide (host, USB, real-time priority, radios, service): [docs/INSTALL.md](docs/INSTALL.md).

```bash
# Debian 12+ / Ubuntu 24.04+: the package (dependencies pulled by apt, C kernels built for this CPU)
./packaging/build-deb.sh && sudo apt install ./mini-cmts_*_all.deb
sudo mini-cmts --config /etc/mini-cmts/mini-cmts.toml          # then power the modem on

# or from the sources
sudo prlimit --pid $$ --rtprio=99:99 --nice=40:40               # real-time priority for this shell
python3 mini_cmts.py --all-in-one --config configs/b210.toml
```

`--all-in-one` also starts `provision.py` (DHCP 10.10.0.10-99, TFTP with a generated config file,
ToD) on the TAP interface `cmts0` (10.10.0.1/24). Without it, run your own DHCP/TFTP/ToD
servers on `cmts0`, like a classic DOCSIS install. Expected log: `RANGING TERMINÉ`, DHCP,
TFTP, `REG-ACK ok -> MODEM EN LIGNE` within about a minute. The console and logs are in French.

- **Bench files** (`--config`, TOML): the keys are the options below (`--ds-channels` ->
  `ds_channels`), `[provisioning]` holds those of `provision.py`, the command line wins over the
  file, a misspelt key is refused. Examples in `configs/`: `b210.toml` (6 DS + 2 US),
  `2xb210.toml`, `bladerf.toml`.
- **Status page**: <http://127.0.0.1:8090/> while it runs: throughput over 30 min, radios
  (underflows, late packets, TX margin), downstream channels (fill), upstream channels (noise,
  receiver delay), modems (state, timing, power, MER), recent alerts. `--status-http
  0.0.0.0:8090` for the lab network, `none` to turn it off.
  - **Capabilities** of each modem, as it announces them: DOCSIS version, downstream receivers and
    upstream transmitters, BPI+, IPv6, symbol rates, receive channel profiles (REG-REQ-MP); vendor,
    model, serial number, software version, embedded components (eCM, eMTA/eDVA, eRouter) from its
    DHCP; the devices behind it (eRouter, eMTA, PCs).
  - **Configuration**: the common options (radio, number of channels, frequencies, modulations,
    levels) in a form, checked (EuroDOCSIS bands, channels per radio, overlaps), written to the
    `--config` bench file (comments kept, `.bak`), then the CMTS restarts on it. From the machine
    itself only (or `ssh -L 8090:127.0.0.1:8090 host`); `--status-edit on` from anywhere the page
    is reachable, `off` read-only.
- **Service**: `sudo systemctl enable --now mini-cmts` runs the bench of
  `/etc/mini-cmts/mini-cmts.toml`; `journalctl -u mini-cmts -f` shows its console.

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
| `--sdr`, `--sdr-args` | b210, — | `bladerf`: a **bladeRF 2.0 micro** instead of the B210 (libbladeRF called directly, `cmts/sdrdev.py`), TX1 to the modem, RX1 for the upstream. Validated with **3 DS + 1 US** (138 / 24.5 Mbit/s): TX and RX at one sample rate (27.8 MS/s at most in 16 bits), RX from 70 MHz (one upstream channel, at 61.8 MHz). `--sdr-args "*:serial=…"` picks the board. Choices, pitfalls and limits: `docs/BLADERF.md`; check: `tools/bladerf_check.py --loop` |
| `--ds-radios`, `--radio-serials`, `--clock-source` | 1, —, internal | several radios (branch `multi-b210`): the downstream channels split into adjacent blocks, one radio per block; the first one also receives the upstream. Without a common 10 MHz (`internal`), the follower realigns on the primary by groups of 8 MPEG packets (drift measured between two B210s: 2.5 ppm, 8.9 ms/h); 2 B210s with 2 + 2 DS: 189 / 50 Mbit/s |
| `--config` | — | a bench file in TOML (above); `@file.args` (one option per line) still works |
| `--status-http` | 127.0.0.1:8090 | the status page; `none`: off |
| `--status-edit` | auto | the Configuration form: changes from this machine only; `on` from anywhere, `off` read-only |
| `--prov-args` | — | e.g. `"--us-burst 64000 --us-rate 1000000000"` (already the defaults; 1 Gbit/s both ways); `--us-buffer` (default 128 KB) sets the modem's upstream buffer (TLV 24.35): ping during an upload 237 -> 93 ms |

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
  TLV 52, as a real CMTS does (read off an ARRIS CMTS, receive only, `tools/origin_maps.py`). It then sends its data as
  segments (8-byte segment header, sequence number) on both channels; the CMTS puts them back in
  order and cuts them into MAC frames again (Continuous Concatenation and Fragmentation). Requests
  are in bytes (queue-depth based requests) and only carry new bytes: the CMTS adds them up, grants
  them on every ranged channel and signals what it still owes with a Data Grant Pending.
- **Several radios** (`--ds-radios 2`, branch `multi-b210`, `cmts/radio.py`):
  - each radio transmits a block of adjacent channels in a process of its own; the first one also receives the upstream;
  - the MAC numbers the blocks; a follower sets its clock on the primary's and timestamps its block k as the primary does;
  - without a common 10 MHz the oscillators drift (2.5 ppm measured between two B210s, 8.9 ms/h, `tools/clock_drift.py`):
    every radio fits a line of its clock against the host's, and the follower inserts or drops 8 null MPEG packets
    (one J.83-A randomization cycle, ~235 µs) when it slides away from the primary;
  - 2 B210s with 2 + 2 DS: 186-189 / 50 Mbit/s for 86 min, with 12.4 ms of correction in total.
- **Bridge** (`cmts/bridge.py`): Ethernet frames between the modems and the TAP interface. Frames
  from Linux are read in batches by a C function (all those waiting, in one call) and turned into
  downstream PDUs by the thread that builds the TS blocks, a byte budget per block: a TCP window
  arriving at once can no longer hold the GIL away from the radios (it stalled both B210s at the
  start of every download).
- **Provisioning** (`provision.py`, `cmts/cmcfg.py`): DHCP, TFTP, ToD, config file generator.

## Tools to analyse the modem's origin network (receive only)

To see how a real CMTS drives a modem (what made upstream bonding work, see below), the downstream
of the network the modem comes from can be recorded and decoded, **receive only**: the B210 or a
HackRF on a coupler, its TX port unconnected, the lab CMTS stopped.

| Tool | What |
|---|---|
| `tools/origin_rx.sh` | records the downstream with `uhd_rx_cfile` (no transmit stream) |
| `origin_listen.py` | decodes the recording in parallel: SYNC, UCD, MAP, MDD and the messages for one modem's MAC, to a pcap |
| `origin_raw.py` | the raw decoded bytes, to compare copies of a message |
| `tools/origin_maps.py` | upstream burst profiles (UCD) and MAP usage per channel, from that pcap |
| `tools/hackrf_us.sh`, `tools/hackrf_reboot_cap.sh` | the same with a HackRF (upstream band, a modem restart) |

Only management messages are kept. A raw recording also holds other subscribers' traffic: do not
keep or share it. Never transmit on that network.

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
python3 tests/run.py            # every test that needs no radio and no root (GitHub Actions too)
python3 tests/run.py --sudo     # also test_provision_tap.py (creates cmts0, runs provision.py)
```

Most tests replay real bursts of the modem (`tests/data/sagem3686/`); GNU Radio (gr-dtv) and
tshark give independent cross-checks of the downstream. On the bench: `docs/TESTS.fr.md`,
`tools/burst_test.sh` (downloads after a quiet spell), `tools/arg_effects.py` (a second B210
checks what each option changes on the air).

## Roadmap

- A common 10 MHz / PPS for several B210s (`--clock-source external`): nothing left to realign;
  then 3 + 3 DS and more radios on a host with enough memory bandwidth.
- bladeRF: 8-bit samples (libbladeRF 2.5+) for 2 US and 4-6 DS; a radio driver per radio, to mix
  B210s and bladeRFs.
- More modems at once (tested with one), and a second modem model.
- Upstream: less CPU per channel (phase tracking by blocks, SIMD Reed-Solomon), more than 2 channels
  in production.

## Licence

**AGPL-3.0-only, or a commercial licence** (dual licensing): see `COMMERCIAL.md`. Free to use,
modify and share; a modified version that is distributed or used over a network must come with its
source. Building it into a closed product, or keeping your changes private, needs a commercial
licence from the copyright holder. Contributions: `CONTRIBUTING.md`. Copyright © 2026 BILLY Rémi.

Specifications referred to (not included): CableLabs DOCSIS MULPIv3.0 and PHYv3.0, ITU-T J.83,
ETSI EN 300 429.
