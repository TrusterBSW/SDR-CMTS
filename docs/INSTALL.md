# Installing mini-cmts

*[Version française : INSTALL.fr.md](INSTALL.fr.md)*

Two ways: the Debian package (Debian 12 or newer, Ubuntu 24.04 or newer), or the sources (any
Linux with Python 3.11+, UHD and a C compiler). Both need the same hardware and the same care with
the cabling: read the **Safety** part of the README first. Closed coax only.

## 1. The host

- **CPU**: what the downstream costs grows with the number of channels per radio:
  - 1 to 3 DS + 1 US: a 4-core laptop;
  - 6 DS + 2 US on one B210: a 6-core desktop (i5-9500, single-thread performance matters more
    than core count);
  - 2 B210s with 2 + 2 DS: tested on a 16-core Threadripper 2950X.
- **USB 3**: each B210 on a USB 3 controller of its own, shared with nothing else (no sound card,
  no other SDR, no hub). `lsusb -t` shows the tree. Late or missing samples (`U`, `L`, `O` in the
  status line) usually come from a shared controller, not from the software.
- **Real-time priority**: the radio and upstream processes raise their priority themselves (`--rt-prio`,
  50 by default). The Debian package allows it to the `usrp` group (`/etc/security/limits.d/99-mini-cmts.conf`)
  and to the service; from the sources: `sudo prlimit --pid $$ --rtprio=99:99 --nice=40:40` in the
  shell that starts it, or the same limits.d lines.
- `sudo` without a password for the user that starts it, or run it as root: it creates the TAP
  `cmts0`, moves interrupts away from the B210's USB core, sets the CPU governor, runs
  `provision.py` (DHCP, TFTP, ToD on privileged ports).

## 2a. The Debian package

```bash
./packaging/build-deb.sh            # -> mini-cmts_<version>_all.deb (needs only dpkg-deb)
sudo apt install ./mini-cmts_*_all.deb
sudo uhd_images_downloader -t b2xx  # B210 FPGA images, once (uhd-host)
```

`apt` pulls the dependencies: Python 3.11+, NumPy, SciPy, gcc (the C kernels are compiled at
installation for this CPU, AVX2 included), iproute2, sudo; recommended: `python3-uhd`, `uhd-host`;
suggested: `bladerf`, `libbladerf2`, `bladerf-fpga-hostedxa4` for a bladeRF, `tshark`, `iperf3`.

What it installs:

| Path | What |
|---|---|
| `/usr/bin/mini-cmts` | the CMTS (`mini_cmts.py`), same options |
| `/usr/lib/mini-cmts/` | the code, `tools/bladerf_check.py`, `tools/clock_drift.py` |
| `/etc/mini-cmts/mini-cmts.toml` | the bench of the service (kept on upgrade) |
| `/usr/share/mini-cmts/configs/` | examples: `b210.toml` (6 DS + 2 US), `2xb210.toml`, `bladerf.toml` |
| `mini-cmts.service` | the systemd service, **neither enabled nor started** by the installation |
| `/var/lib/mini-cmts/captures/` | the logs and captures of the service |

Moved the disk to another CPU? `sudo dpkg-reconfigure mini-cmts` compiles the C kernels again.

## 2b. From the sources

```bash
# Debian / Ubuntu
sudo apt install python3-numpy python3-scipy python3-uhd uhd-host gcc iproute2
# Arch Linux
sudo pacman -S python-numpy python-scipy libuhd gcc iproute2
sudo uhd_images_downloader -t b2xx
git clone <this repository> && cd mini-cmts
python3 tests/run.py                # every test that needs no radio (a few minutes)
```

The C kernels (`cmts/*.c`) are compiled on first use into `cmts/_*.so`; without a C compiler the
Python fallbacks are far too slow for a radio.

## 3. The radio

- **B210**: `uhd_find_devices` must list it, on USB 3 (`uhd_usrp_probe` says `USB 3`).
  A B210 clone with its own FPGA image (e.g. LibreSDR) gets it in `~/.config/uhd.conf`:

  ```ini
  [serial=JN76WAJ]
  fpga=usrp_b210_fpga_libresdr.bin
  ```
- **bladeRF 2.0 micro**: `python3 tools/bladerf_check.py --loop` checks the board, the versions,
  USB 3, the gains and 10 s of TX + RX. Its FPGA must match libbladeRF: see `docs/BLADERF.md`.
- **Wiring**: see the README. Never the RX input without the 8 dB attenuator.

## 4. First start

```bash
sudo mini-cmts --config /etc/mini-cmts/mini-cmts.toml     # package
python3 mini_cmts.py --all-in-one --config configs/b210.toml   # sources
```

Then power the modem on. Within about a minute: `RANGING TERMINÉ`, DHCP, TFTP,
`REG-ACK ok -> MODEM EN LIGNE`. The PC reaches what is behind the modem through `cmts0`
(10.10.0.1/24). Ctrl+C stops it cleanly; power the modem off first.

The status page, on the same machine: <http://127.0.0.1:8090/> (channels, radios, modems and what
they announce, throughput, recent alerts, and a form for the common options that rewrites the bench
file and restarts the CMTS). `--status-http 0.0.0.0:8090` to read it from the lab network (the form
stays read-only from there unless `--status-edit on`; from another PC: `ssh -L
8090:127.0.0.1:8090 <host>` then <http://127.0.0.1:8090/>), `none` to turn it off.

## 5. As a service

```bash
sudoedit /etc/mini-cmts/mini-cmts.toml          # the bench: radio, channels, provisioning
sudo systemctl enable --now mini-cmts
journalctl -u mini-cmts -f                      # the console of the CMTS
sudo systemctl stop mini-cmts                   # Ctrl+C for the CMTS: radios stopped cleanly
```

It restarts after a failure (15 s later). The log of every run is also in
`/var/lib/mini-cmts/captures/cmts_<date>_<time>.log`.

## 6. The bench file

Keys are the options of `mini-cmts --help` (`--ds-channels` -> `ds_channels`); sections only group
them, except `[provisioning]` (the options of `provision.py`). The command line wins over the file:

```bash
mini-cmts --config /etc/mini-cmts/mini-cmts.toml --ds-channels 2 --status-http none
```

A misspelt key is refused with the nearest names, rather than silently keeping a default.
