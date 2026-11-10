# Options and why they are set this way

*[Version française : OPTIONS.fr.md](OPTIONS.fr.md)*

Every option is in `man sdr-cmts` and `sdr-cmts --help`; this page keeps, for the main ones, the measurement that chose the default.

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
| `--synth-threads` | 2, raised by itself | threads sharing the J.83 encoding and shaping of the downstream channels (at most one per channel: more changes nothing). From 4 channels: one per 2 channels, or one per channel when the host has at least channels + 4 physical cores (Xeon E5-2630 v4, 10 cores at 2.4 GHz: ~0.6 core per channel, 3 threads of 2 channels fell behind at 6 DS; i5-9500, 6 cores: 3 threads) |
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
