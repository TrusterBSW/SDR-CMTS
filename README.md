
<h1 align="center">sdr-cmts</h1>

<p align="center">
  <b>Your own cable network on your own coax.</b><br>
  A EuroDOCSIS 3.0 CMTS on a software radio: real cable modems online, bonded, at full speed.
</p>

<p align="center">
  <a href="README.fr.md">Version française</a> ·
  <a href="#install">Install</a> ·
  <a href="#quick-start">Quick start</a> ·
  <a href="#documentation">Documentation</a>
</p>

<p align="center">
  <img alt="license" src="https://img.shields.io/badge/license-AGPL--3.0--only%20%7C%20commercial-blue">
  <img alt="platform" src="https://img.shields.io/badge/platform-Debian%2012%20%7C%2013%20%7C%20Ubuntu%2024.04-informational">
  <img alt="standard" src="https://img.shields.io/badge/EuroDOCSIS-3.0-success">
  <img alt="radios" src="https://img.shields.io/badge/radios-USRP%20B210%20%7C%20bladeRF%202.0-orange">
</p>

---

sdr-cmts is a Cable Modem Termination System, the head-end of a cable network, written in Python with C
kernels. It drives one or more Ettus **USRP B210** (or a Nuand **bladeRF 2.0 micro**) and brings
real, unmodified cable modems online on a closed coax: the modem ranges, registers, gets its address
and configuration file, and the PC behind it browses the Internet through it. As far as we know, it is
the first open-source CMTS running on a low-cost software radio.

- **Full EuroDOCSIS 3.0**: up to 6 bonded 256-QAM downstream channels per B210, upstream ATDMA up
  to 64-QAM at 5.12 Msym/s with DOCSIS 3.0 upstream bonding (Multiple Transmit Channel mode),
  ranging, registration (DOCSIS 1.1 to 3.0), CoDel on the downstream queues.
- **At the speed of the channels**: 283-284 Mbit/s down and 48-52 Mbit/s up through a Sagem F@st 3686
  with one B210, ~75 ms of ping.
- **Several radios**: two B210s share the downstream channels, the follower realigned on the
  primary without a common 10 MHz; or a bladeRF 2.0 micro (3 DS + 1 US).
- **Everything a modem needs**: DHCP, TFTP with a generated configuration file, time of day, and an
  Ethernet bridge (TAP `cmts0`) to the devices behind the modems.
- **A status page**: channels, radios, upstream receivers, modems and what they announce (DOCSIS
  version, receivers, transmitters, vendor, model, embedded eMTA and eRouter), throughput, alerts,
  and a form for the common options.
- **Ready to run**: one Debian package, a systemd service, the host settings put back after every
  stop, a simulated radio to try it, or size a PC, without any hardware.

> [!WARNING]
> **Closed coax only.** A CMTS transmits: connected to an operator's cable network it would
> transmit on frequencies you have no licence for and disturb other subscribers. sdr-cmts is meant
> for a private coax between the radio and your own modems, with attenuators.

**Status: experimental lab tool.** Validated with one modem: a Sagem F@st 3686 (DOCSIS 3.0) running
the SFR operator firmware (FAST3686_SFR-PC20_3.649.0); every measurement below was made with it.
**EuroDOCSIS only** (8 MHz channels, ITU-T J.83 annex A): North American DOCSIS (annex B, 6 MHz
channels) is not implemented.

## Contents

- [Install](#install)
- [Quick start](#quick-start)
- [How it works](#how-it-works)
- [Configuration](#configuration)
- [Hardware and wiring](#hardware-and-wiring)
- [Web page](#web-page)
- [Service and host settings](#service-and-host-settings)
- [Standards and radios](#standards-and-radios)
- [Measurements](#measurements)
- [Lessons learned](#lessons-learned)
- [Tools for the modem's origin network](#tools-for-the-modems-origin-network)
- [Documentation](#documentation)
- [Building from source](#building-from-source)
- [Roadmap](#roadmap)
- [License](#license)

## Install

One package for Debian 12 and 13, Proxmox VE 8 and 9, and Ubuntu 24.04 or later. In one line:

```bash
curl -fsSL https://raw.githubusercontent.com/TrusterBSW/SDR-CMTS/main/install.sh | sudo sh
# --uhd-images: also the USRP B210's FPGA images
curl -fsSL https://raw.githubusercontent.com/TrusterBSW/SDR-CMTS/main/install.sh | sudo sh -s -- --uhd-images
```

Or by hand:

```bash
wget https://github.com/TrusterBSW/SDR-CMTS/raw/main/releases/sdr-cmts_0.1.0_all.deb
sudo apt install ./sdr-cmts_0.1.0_all.deb
sudo uhd_images_downloader -t b2xx            # the B210's FPGA images, once
```

The package is `Architecture: all`: Python and C sources, the C kernels compiled at installation for
the CPU of the machine (AVX2 where there is). apt pulls Python, NumPy, SciPy, gcc and UHD; libbladeRF
is suggested. `packaging/build-all-debs.sh` builds it in a clean container and installs and checks it
in a fresh container of each release; GitHub Actions does the same on each tag and publishes it on the
[Releases](https://github.com/TrusterBSW/SDR-CMTS/releases) page.

The host, USB, real-time priority and radios in detail: [`docs/INSTALL.md`](docs/INSTALL.md).

## Quick start

```bash
uhd_find_devices                                    # 1. the B210, on USB 3
sudoedit /etc/sdr-cmts/sdr-cmts.toml                # 2. the bench: radio, channels, provisioning
sudo sdr-cmts --config /etc/sdr-cmts/sdr-cmts.toml  # 3. on the air; then power the modem on
xdg-open http://127.0.0.1:8090/                     # 4. the status page
sudo systemctl enable --now sdr-cmts                # 5. as a service, now and at every boot
```

Within about a minute the log shows `RANGING TERMINÉ`, DHCP, TFTP, then `REG-ACK ok -> MODEM EN
LIGNE`, and the PC behind the modem gets an address (the console and the logs are in French). To try
without any radio:

```bash
SDR_CMTS_SDR_SIM=1 sdr-cmts --tap none --ds-channels 4     # the whole chain on a simulated radio
```

`sdr-cmts --help` lists the options, `man sdr-cmts` has everything.

## How it works

```
 the coax                 radio process (one per radio)              MAC process (sdr_cmts.py)
 ────────                 ─────────────────────────────              ──────────────────────────────────
                          ┌ J.83 A encoding, RRC shaping,        ┌ SYNC, UCD, MAP, MDD, data PDUs
 downstream ◄── B210 TX ◄─┤ channels shifted and summed (C) ◄────┤ TS blocks, CoDel queues
                          └ timed send (UHD / libbladeRF)        │ ranging, registration, requests,
                                                                 │ grants, bonding (RCC, DSID, TCC)
 upstream ───► B210 RX ──► sample ring (shared memory)           │
                             └► one process per upstream ───────►└ bridge: TAP cmts0 ◄──► Linux
                                channel: DDC, burst demod (C)       provision.py: DHCP / TFTP / ToD
                                                                    status page :8090
```

- **The downstream is the time base**: every block of TS packets carries the device time it goes on
  the air at, so every DOCSIS timestamp maps to an exact upstream sample. The receiver knows where
  each MAP interval lands and measures each modem's timing error to a few nanoseconds.
- **The heavy work is in C** (J.83 encoding, synthesis of the bonded channels in AVX2, DDC,
  burst demodulation, Reed-Solomon, TS packing, TAP reads), in processes of their own: the MAC's
  Python never holds back the radios.
- **Upstream bonding as a real CMTS does it**: Multiple Transmit Channel mode, a Transmit Channel
  Configuration with its Dynamic Range Window, SID clusters, queue-depth requests added up, segments
  reassembled into MAC frames.
- **Several radios without a common clock**: each radio fits a line of its clock against the host's,
  and a follower inserts or drops groups of 8 null MPEG packets (one J.83-A randomisation cycle,
  ~235 µs) when it slides away from the primary (2.5 ppm measured between two B210s).

Each step, measurement and bug found: [`docs/JOURNAL.fr.md`](docs/JOURNAL.fr.md) (in French).

## Configuration

One TOML file, `/etc/sdr-cmts/sdr-cmts.toml`. The keys are the long options (`--ds-channels` becomes
`ds_channels`), the sections only group them, except `[provisioning]`, whose keys are those of the
provisioning server. The command line wins over the file; a misspelt key is refused with the nearest
names.

```toml
[radio]
sdr = "b210"                # or "bladerf"
args = "type=b200"          # UHD device arguments (",serial=..." to pick one)

[downstream]
freq = 306e6                # primary channel (European grid 114 + 8n MHz)
ds_channels = 6             # bonded 8 MHz channels, up to 6 per B210
ds_layout = "around"        # the others on both sides of the primary
qam = 256

[upstream]
us_channels = 2             # 60 and 53 MHz: DOCSIS 3.0 upstream bonding
us_target = -20             # dBFS of the ranged bursts

[provisioning]              # DHCP, TFTP, ToD on the cmts0 bridge
dns = "1.1.1.1"

[host]
all_in_one = true           # the provisioning server too
status_http = "127.0.0.1:8090"
```

Examples in [`configs/`](configs/): `b210.toml` (6 DS + 2 US), `2xb210.toml` (two B210s),
`bladerf.toml`. Every option is in `man sdr-cmts`; the measurements behind the defaults in
[`docs/OPTIONS.md`](docs/OPTIONS.md).

## Hardware and wiring

- A **USRP B210** on a USB 3 controller of its own, shared with nothing else; or a bladeRF 2.0 micro.
- A PC: 3 DS + 1 US run on a 4-core laptop, 6 DS + 2 US on a 6-core i5-9500.
- One 1:2 splitter, one 8 dB attenuator, a second splitter to monitor.

```
B210 TX/RX ── splitter A [in] ──┬── out 1 ── cable modem
                                └── out 2 ── 8 dB ── splitter B [in] ──┬── B210 RX2
                                                                        └── (optional receiver)
```

The modem's upstream reaches RX2 through the isolation between the outputs of splitter A. **Never
connect RX2 without attenuation**: a modem transmits up to +60 dBmV. Two B210s: their TX/RX ports on
a 1:2 coupler, their RX2 ports on another one (behind the 8 dB attenuator), both couplers and the
modem on the outputs of a 1:3 coupler. Start the CMTS, then power the modem; power the modem off
before stopping it.

## Web page

`http://127.0.0.1:8090/` while it runs (`status_http = "0.0.0.0:8090"` for the lab network):

- throughput over the last 30 minutes, radios (underflows, late packets, transmit margin), downstream
  channels and their fill, upstream channels (noise, receiver delay), recent alerts;
- each modem: state, timing, power, MER, and what it announces in its registration and its DHCP
  (DOCSIS version, receivers and transmitters, BPI+, IPv6, vendor, model, serial number, software
  version, embedded eMTA / eDVA / eRouter), the devices behind it;
- a **Configuration** form for the common options (radio, channels, frequencies, modulations,
  levels), checked against the EuroDOCSIS bands, written to the bench file (`.bak` kept) and the CMTS
  restarted on it. From the machine itself only (or `ssh -L 8090:127.0.0.1:8090 host`);
  `--status-edit on` from anywhere the page is reachable.

`/status.json` gives the same data, `/source.tar.gz` the program's source.

## Service and host settings

```bash
sudo systemctl enable --now sdr-cmts      # the bench of /etc/sdr-cmts/sdr-cmts.toml
journalctl -u sdr-cmts -f                 # its console
```

- An SDR unplugged stops the CMTS (exit code 3); the service starts it again every 15 s until the SDR
  is back.
- For a run the CMTS sets the CPU governor to `performance`, blocks the deep C-states and moves the
  other interrupts off its cores. After every stop, a killed one too, they go back to the kernel's
  defaults (`sdr-cmts --restore-host`, run by the service).
- Logs and captures: `/var/lib/sdr-cmts/captures/`.

## Standards and radios

| | |
|---|---|
| Downstream | EuroDOCSIS, J.83 annex A, 8 MHz, 6.952 Msym/s, 64/256-QAM, 1-6 bonded channels per B210 |
| Upstream | TDMA (DOCSIS 1.x) or ATDMA (2.0), QPSK to 64-QAM, 1.28 to 5.12 Msym/s, 5-65 MHz, 1-4 channels |
| MAC | DOCSIS 1.1 to 3.0 registration, MDD, RCC, DSID, Multiple Transmit Channel mode, CM-STATUS |
| USRP B210 | UHD 4.3 to 4.9; up to 6 DS + 2 US on one; two B210s share the downstream |
| bladeRF 2.0 micro | libbladeRF, 3 DS + 1 US; choices and limits in [`docs/BLADERF.md`](docs/BLADERF.md) |
| Not implemented | North American DOCSIS (annex B), DOCSIS 3.1 (OFDM), BPI+ encryption |

## Measurements

Sagem F@st 3686, iperf3 through the modem:

| Setup | Result |
|---|---|
| One B210, 6 DS 256-QAM (290-330 MHz) + 2 US ATDMA 64-QAM, i5-9500 | **283-284 Mbit/s** down (8-16 flows), **48-52** up (8 flows), ping ~75 ms |
| Same, 4 downloads after a quiet spell | no transmit stall, no lock lost |
| Same, speedtest from the Internet (the host doing NAT) | 205-228 Mbit/s down, no incident |
| Two B210s, 2 + 2 DS + 2 US, no common 10 MHz, Threadripper 2950X | **188-189 / 49.5 Mbit/s**, 86 min with the follower realigned (12.4 ms) |
| bladeRF 2.0 micro, 3 DS + 1 US | **138 / 24.5 Mbit/s** |
| One B210, 4 DS + 2 US, Xeon E5-2630 v4 (10 cores at 2.4 GHz), decoding threads | **186 / 51.5 Mbit/s** (8 flows) |
| Each option's effect, seen by a second B210 (frequency, level, QAM, spectrum, MDD, UCD) | all as expected (`tools/arg_effects.py`) |

Close to the physical limits of the channels (6 × 256-QAM ≈ 300 Mbit/s, 2 × 64-QAM ATDMA ≈ 50 Mbit/s).
Every test with its date: [`docs/TESTS.fr.md`](docs/TESTS.fr.md); the earlier steps:
[`docs/HISTORY.md`](docs/HISTORY.md).

## Lessons learned

- **Real time on a PC**: real-time priority, the B210's USB interrupt core left free, the other
  interrupts (a touchpad, the network card under a speedtest) moved away, no file written from the
  transmit path, Python's garbage collector frozen at start.
- **The modem's Max Traffic Burst** (config file TLV 24.9) caps each request: the default 3044 bytes
  limited the upload to ~3.5 Mbit/s.
- **Upstream bonding with the Sagem**: a channel above 65 MHz gets REG-ACK 209; the Dynamic Range
  Window, mandatory in MTC mode, is only accepted in the form a real CMTS sends it (action "change",
  nothing in the RNG-RSPs), which listening to one, receive only, showed. Multi-channel requests only
  carry new bytes: they must be added up, not replaced.
- **UHD calibrates the AD9361 TX quadrature** when the streams open, through the RX analog filter:
  with that filter at the upstream width the modem never locked above 34.76 MHz of master clock. The
  streams now open with a 56 MHz RX filter, narrowed afterwards.
- **The modem's upstream symbol clock was 4.3 ppm off**: harmless on short bursts, fatal on 3 ms ones.
- **A multiprocessing.Event waited on by a process that dies** deadlocks whoever sets it: an SDR
  unplugged froze the whole CMTS until a shared flag replaced it.

## Tools for the modem's origin network

To see how a real CMTS drives a modem, the downstream of the network the modem comes from can be
recorded and decoded, **receive only**: the radio on a coupler, its TX port unconnected, the lab CMTS
stopped.

| Tool | What |
|---|---|
| `tools/origin_rx.sh` | records the downstream with `uhd_rx_cfile` (no transmit stream) |
| `origin_listen.py` | decodes the recording: SYNC, UCD, MAP, MDD and the messages for one modem's MAC, to a pcap |
| `origin_raw.py` | the raw decoded bytes, to compare copies of a message |
| `tools/origin_maps.py` | upstream burst profiles (UCD) and MAP usage per channel, from that pcap |
| `tools/hackrf_us.sh`, `tools/hackrf_reboot_cap.sh` | the same with a HackRF |

Only management messages are kept. A raw recording also holds other subscribers' traffic: do not keep
or share it. Never transmit on that network.

## Documentation

| Page | What it covers |
|---|---|
| `sdr-cmts --help`, [`man sdr-cmts`](man/sdr-cmts.1) | every option, the bench file, the status page, files, exit codes |
| [`docs/INSTALL.md`](docs/INSTALL.md) | the host, USB, real-time priority, the radios, the service, trying without a radio |
| [`docs/OPTIONS.md`](docs/OPTIONS.md) | the main options and the measurements behind their defaults |
| [`docs/BLADERF.md`](docs/BLADERF.md) | the bladeRF 2.0 micro: choices, pitfalls, limits |
| [`docs/JOURNAL.fr.md`](docs/JOURNAL.fr.md) | the whole story: each step, measurement and bug (French) |
| [`docs/TESTS.fr.md`](docs/TESTS.fr.md) | every bench test with its date and result (French) |
| [`docs/HISTORY.md`](docs/HISTORY.md) | the results of the earlier steps |

Installed with the package in `/usr/share/doc/sdr-cmts/`; from the sources, `man -l man/sdr-cmts.1`.

## Building from source

```bash
sudo apt install python3-numpy python3-scipy python3-uhd uhd-host gcc iproute2
python3 tests/run.py                          # every test that needs no radio (a few minutes)
sudo prlimit --pid $$ --rtprio=99:99 --nice=40:40
python3 sdr_cmts.py --all-in-one --config configs/b210.toml     # from here, as root or with sudo
./packaging/build-deb.sh                      # the .deb (needs only dpkg-deb)
./packaging/build-all-debs.sh                 # built and checked in every release (docker)
python3 packaging/gen_man.py --write          # man/sdr-cmts.1, after changing an option
```

Python 3.11 or later. The C kernels (`cmts/*.c`) are compiled on first use; without a compiler the
Python fallbacks are far too slow for a radio.

## Roadmap

Wanted next:
- **A software cable modem (SDR CM)**: the modem side of DOCSIS on a second radio, to test the CMTS
  against something fully known. In the lab, against this CMTS only.
- **ZeroMQ between the CMTS and the SDR CM**: the samples exchanged in software instead of over the
  air, as srsRAN does for LTE: the whole DOCSIS link on one PC with no radio, for development and CI.
- **A CMTS spread over several PCs**: radios (and their USB traffic) on separate hosts, the MAC on
  one, the clocks tied together (10 MHz / PPS, or PTP).

Other possible evolutions:
- A common 10 MHz / PPS for several B210s (`--clock-source external`), then 3 + 3 DS and more radios.
- North American DOCSIS: J.83 annex B downstream; the upstream and the MAC are already the same.
- bladeRF: 8-bit samples (libbladeRF 2.5+) for 2 US and 4-6 DS; B210s and bladeRFs mixed.
- More modems at once, a second modem model; BPI+ and IPv6 provisioning.
- Offloading the CPU (measured on the i5-9500, 2026-11-01):
  - **integrated GPU** (OpenCL, `tools/gpu_bench.py`): the synthesis of 6 channels, 0.80 core in
    AVX2, takes 0.62 ms per 1.88 ms block on an Intel UHD 630, 0.32 core left on the CPU; ~0.7 core
    saved with zero-copy buffers, 10-15 % of a 6 + 2 run;
  - **the B210's FPGA**: the 12-bit USB format (`sc12`) cuts the USB traffic by a quarter; the FPGA's
    DDCs on two RX paths cap the master clock at 30.72 MHz (3 DS); a custom "edge QAM" image (J.83,
    QAM, shaping and mixing in the FPGA, as real CMTSs do) would remove ~1.3 cores and ~90 % of the
    TX USB traffic, at the cost of months of Spartan-6 work;
  - **the MAC's data path in C**: done for the downstream (2026-11-10, `--ds-dataplane`, on by
    default): frame -> PDU, channel, queues with CoDel, TS packets in `cmts/dsdsp.c`, the same TS
    packets byte for byte; 0.25 -> 0.07 core at 290 Mbit/s of full-size frames, 2 cores (falling
    behind) -> 0.10 for small ones (`tools/ds_path_bench.py`). The upstream frames (~4,000/s at
    50 Mbit/s, a few µs each: ~0.04 core) and the control plane stay in Python.

## License

**AGPL-3.0-only, or a commercial licence** (dual licensing, [`COMMERCIAL.md`](COMMERCIAL.md)): free to
use, modify and share; a modified version distributed or used over a network must come with its
source; building it into a closed product, or keeping your changes private, needs a commercial
licence. Contributions: [`CONTRIBUTING.md`](CONTRIBUTING.md). © 2026 BILLY Rémi.

DOCSIS® and EuroDOCSIS™ are trademarks of Cable Television Laboratories, Inc. (CableLabs); this
project is not affiliated with, certified or endorsed by CableLabs. Specifications referred to (not
included): CableLabs DOCSIS MULPIv3.0 and PHYv3.0, ITU-T J.83, ETSI EN 300 429.
