# mini-CMTS — a software EuroDOCSIS CMTS on one USRP B210

*[Version française : README.fr.md](README.fr.md)*

A Cable Modem Termination System written in Python (with C kernels) that drives a single
Ettus/NI **USRP B210** and brings a real, unmodified cable modem **online** in a closed lab:
EuroDOCSIS downstream, DOCSIS 1.x/2.0 upstream, ranging, DHCP/TFTP/ToD, registration
(DOCSIS 1.1 and 3.0), downstream channel bonding and an Ethernet bridge to the Linux host.

As far as we know there was no open-source CMTS able to do this with a low-cost SDR: this project
exists to learn DOCSIS hands-on and to let others do the same.

**Status: experimental lab tool.** Validated with one modem (Sagem F@st 3686, DOCSIS 3.0).

## Results (Sagem F@st 3686, 2026-10-10)

| | Configuration | Measured |
|---|---|---|
| Downstream | 2 bonded channels, EuroDOCSIS 256-QAM (306 + 314 MHz) | **93 Mbit/s** (speedtest on the CMTS host) |
| Upstream | 1 ATDMA channel, 64-QAM, 5.12 Msym/s (60 MHz) | **22 Mbit/s** |
| Latency | ping through the modem | 56-72 ms idle, 92-139 ms loaded |

Both are the physical limits of the channels used (2 × 256-QAM ≈ 100 Mbit/s;
1 × 64-QAM ATDMA ≈ 25 Mbit/s after FEC and burst overhead).

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
| `--ds-channels` | 2 | 1 to 6 (master clock 27.808 / 41.712 / 55.616 MHz; 3+ experimental) |
| `--qam` | 256 | downstream QAM |
| `--us-freq`, `--us-rate`, `--us-mode`, `--us-mod` | 60 MHz, 32 (5.12 Msym/s), atdma, 64qam | upstream channel |
| `--us-target` | -14 dBFS | burst level the modems are ranged to |
| `--grant-ahead-hold/-every/-scale` | on, 3, 4 | unsolicited grants keeping the upload going |
| `--us-process` | on | upstream demodulation in its own process |
| `--prov-args` | — | e.g. `"--us-burst 64000 --us-rate 200000000"` (already the defaults) |

`python3 mini_cmts.py --help` lists everything; `--sim SECONDS` runs a simulated modem without
hardware.

## How it works

- **Downstream** (`cmts/downstream.py`, `cmts/j83a.py`, C: `cmts/dsdsp.c`): MPEG-TS multiplexer
  carrying SYNC, UCD, MAP, MDD and data, J.83 Annex A encoder (RS, interleaver, randomizer,
  256-QAM), RRC shaping; bonded channels shifted and summed in one C pass, sent to the B210 at
  4/6/8 samples per symbol. Every block is time-stamped: the downstream is the time base.
- **Upstream** (`cmts/upstream.py`, `cmts/us_phy.py`, C: `cmts/usdsp.c`, process: `cmts/usproc.py`):
  the RX stream is cut into the MAP intervals; per burst: preamble search, symbol interpolation,
  carrier, gain, equalizer cached per modem, symbol-clock offset compensation per modem,
  decision-directed tracking, descrambling, Reed-Solomon, MAC frames and concatenations.
- **MAC** (`cmts/ranging.py`, `cmts/registration.py`, `cmts/docsis_mac.py`): ranging
  (timing/power/frequency), bandwidth requests and grants (including grants in advance),
  REG-REQ(-MP)/REG-RSP(-MP)/REG-ACK with CMTS MIC, receive channel configuration and DSID for
  bonding, MDD for DOCSIS 3.0.
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

Details, measurements and every bug found: `docs/JOURNAL.fr.md` (French) and `docs/TESTS.fr.md`.

## Tests

```bash
for t in tests/test_*.py; do python3 $t | tail -1; done   # test_provision_tap.py needs sudo
```

Most tests replay real bursts of the modem (`tests/data/sagem3686/`).

## Roadmap

- 4 downstream channels in real time (C synthesis done; the CPU of the test laptop is the limit),
  then 5-6.
- DOCSIS 3.0 upstream bonding (several upstream channels).
- Spreading the CMTS over several SDRs / PCs.

## Licence

To be chosen by the project owner before publication.

Specifications referred to (not included): CableLabs DOCSIS MULPIv3.0 and PHYv3.0, ITU-T J.83,
ETSI EN 300 429.
