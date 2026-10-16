# Earlier results

*[Version française : HISTORY.fr.md](HISTORY.fr.md)*

The steps before the current defaults, kept for the record (README: the current results).

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
