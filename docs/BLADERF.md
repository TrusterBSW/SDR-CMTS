# Running the CMTS on a Nuand bladeRF 2.0 micro

The CMTS was written around the Ettus B210 (UHD). It now also runs on a **bladeRF 2.0 micro**
(`--sdr bladerf`): same RF chip (Analog Devices AD9361), different board, FPGA, USB firmware and
host library. This page lists what was chosen to make it work, what it reaches, and where it
stops, as measured on 2026-10-15.

## Result

| | bladeRF 2.0 micro | B210, for reference (measured before) |
|---|---|---|
| Configuration | 3 DS (256-QAM, 6.952 Msym/s) + 1 US (ATDMA 64-QAM, 5.12 Msym/s) | up to 6 DS + 2 US |
| Downstream, 8 and 16 TCP flows | **138 Mbit/s** (the 3 channels full, ~46 each) | ~47 per channel: 283 with 6 DS |
| Upstream, 8 flows | **24.5 Mbit/s** (one channel full) | ~48-52 with 2 channels |
| Ping through the modem | ~144 ms | ~68 ms |
| Upstream MER | 22 dB | 27-30 dB |

Modem: Sagem F@st 3686 (DOCSIS 3.0), online, the 3 channels bonded. Host: Threadripper 2950X,
Debian 12, libbladeRF 2.4.1, bladeRF FPGA 0.14.0.

```bash
python3 tools/bladerf_check.py --loop        # board, versions, USB 3, gains, 10 s TX+RX, TX->RX delay
python3 mini_cmts.py --all-in-one @configs/bladerf.args
```
Wiring: **TX1** to the coax towards the modem (as the B210's TX/RX), **RX1** for the upstream (as
its RX2), through the same couplers and attenuator.

## Choices

- **libbladeRF called directly** (`cmts/sdrdev.py`, `ctypes` on `libbladeRF.so`): no SoapySDR or
  other abstraction layer, and not the `bladerf` Python module either (not packaged on every
  distribution). The radio process talks to a small interface (`UhdRadio`, `BladeRFRadio`), so
  the rest of the CMTS is the same for both boards.
- **One sample rate for TX and RX.** On the bladeRF 2.0 the two directions cannot run at different
  rates: setting one sets both (measured). So: one tick per sample (no master clock above the
  sample rate), no FPGA decimation of the upstream; the upstream processes take each channel out
  of the wideband stream in software (C DDC), as they already did with several upstream channels.
- **Hardware timestamps** (`SC16_Q11_META`): the first TX block opens the burst at its timestamp,
  each next one carries `TX_UPDATE_TIMESTAMP` (zero-padded over a gap), as the B210 path times every
  block. RX: whole buffers read back to back, each asking for the timestamp that follows the last
  one; `RX_NOW` only to start and after a loss. Samples older than the start time are dropped.
- **Device time.** The bladeRF's counters cannot be set: device time = TX counter + an offset kept
  in software. TX and RX counters turned out to be the same clock (a TX burst looped back to RX
  arrives ~3 µs later); reading them one after the other over USB had shown a false 165 µs offset,
  so the offset is now taken from an RX read between two TX reads.
- **Upstream below the RX range.** The bladeRF 2.0 receives from 70 MHz only (it transmits from
  47 MHz). The RX LO sits at 70 MHz and the single upstream channel at 61.8 MHz (58.6-65 MHz,
  EuroDOCSIS stops at 65 MHz): the DDC shifts it back by 8.2 MHz.
- **Gains.** TX 50 dB (libbladeRF scale; the 3 channels at about the B210's level), RX 14 dB: at
  20 dB the modem arrived above the ranging target already at its minimum power, and ranging
  never reached "success".

## Pitfalls met (and handled in the driver)

- **FPGA newer than the library.** The board came with FPGA 0.16.0 in flash (bladeRF 2025.10
  tools); Debian 12's libbladeRF 2.4.1 then misreads the stream metadata: RX buffers of 508
  samples whose timestamps moved by 2044, every read flagged as an overrun, the RX at 7 of
  27.8 MS/s. With the FPGA of the same release (0.14) it works. The driver refuses the mismatch
  with the command to load the matching FPGA until power-off:
  `bladeRF-cli -l /usr/share/Nuand/bladeRF/hostedxA4.rbf` (or install libbladeRF ≥ 2.6).
- **The `bladerf_format` enum changed between versions**: `SC16_Q11_META` is 1 up to libbladeRF
  2.5 and 2 from 2.6 (`SC16_Q11_PACKED` was inserted before it). Chosen from `bladerf_version()`;
  `MINI_CMTS_BLADERF_META_FORMAT` forces it.
- **Gains set before a module is enabled are ignored** (libbladeRF 2.4.1 answers success): set
  again once the modules run; the log prints the gains actually in place.
- **Reading with `RX_NOW` every time** returned the few samples already there (~50 per call):
  the RX fell far behind.
- The device is reset when it is closed; opening it again at once answers "no device": retried.

## Limits

- **3 DS + 1 US on this host.** TX and RX at the same rate, 16-bit samples: 27.8 MS/s both ways
  (222 MB/s of USB) holds; 34.8, 41.7 and 55.6 MS/s (4, 5, 6 DS) do not (TX falling behind, RX
  overruns), measured with `tools/bladerf_check.py --rate`.
- **Two upstream channels** need the RX to see down to ~50 MHz from its 70 MHz LO: about
  50 MS/s or more, hence TX at that rate too.
- **The way to 6 DS + 2 US**: 8-bit samples (`SC8_Q7_META`, libbladeRF ≥ 2.5 with a recent FPGA)
  halve the USB traffic, 55.6 MS/s both ways back to ~222 MB/s. Not done yet. The CPU then
  matters as much: the 2950X used here (memory on one channel) did not keep up with 6 DS even on
  a B210.
- **Higher latency**: libbladeRF's TX buffering (64 × 16384 samples ≈ 38 ms) and the deeper USB
  reserve: ping ~144 ms against ~68 with the B210. To be tuned.
- **Upstream MER 22 dB** (noise at −54 dBFS against −63 on the B210): enough for 64-QAM.
- **No external 10 MHz** handling in this driver yet (a warning), and one `--sdr` for every radio:
  a B210 and a bladeRF cannot yet be mixed in one CMTS (`--ds-radios`).
- **Late TX samples are not reported** by libbladeRF's synchronous interface (only underruns,
  per call): the status line's `L` counter stays at 0 on a bladeRF.
- **bladeRF 1** (LMS6002D): refused for the upstream, it receives from 300 MHz only (the XB-200
  transverter would be needed).
