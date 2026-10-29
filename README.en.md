# xray-bench

**English** · [Русский](README.md)

A reproducible bench for comparing three proxy engines — **Xray-core**, **sing-box** and
**Unbihex** — along the path "SOCKS inbound → REALITY/XHTTP → server → sink": emulated RTT,
throughput, TTFB, connection rate and CPU, with provenance recorded in every result.

The bench answers "which engine is faster under identical conditions", not "how much will a real
link give". The limitations are listed below — read them before quoting the numbers.

## Results

One bench, one run, medians. Self-pair: the client and the server of the same engine, REALITY,
loopback, 16 cores, RTT via `netem` on the tunnel port only. Base `5026c2d` (v0.2.2), 2026-10-29.

| Engine (self-pair) | ↓1, RTT≈0 | ↓8, RTT≈0 | ↑1, RTT≈0 | TTFB | conn/s | ↓1, RTT 50 ms | TTFB, RTT 50 |
|---|---|---|---|---|---|---|---|
| Xray 26.10.29 | 361 MB/s | 806 MB/s | 341 MB/s | 3.95 ms | 448 | 88.6 MB/s | 154 ms |
| sing-box 1.14.4 | 540 MB/s | 1306 MB/s | 530 MB/s | 3.34 ms | 563 | 103.6 MB/s | 154 ms |
| Unbihex 0.2.2 (stock) | 437 MB/s | 1011 MB/s | 386 MB/s | 5.57 ms | 276 | 5.9 MB/s | 156 ms |
| Unbihex 0.2.2 + [PR #18](https://github.com/crystalmasta/unbihex/pull/18) | 430 MB/s | 1163 MB/s | 411 MB/s | 1.98 ms | 938 | 85.8 MB/s | 152 ms |

These are protocol-layer numbers on loopback: compare cells with each other, not with a real link
(see [Limitations](#limitations-important)). "PR #18" is the branch with the TUN, socket-buffer
and REALITY changes from [unbihex#18](https://github.com/crystalmasta/unbihex/pull/18); "stock"
is the released 0.2.2.

## What is measured

| Metric | How |
|---|---|
| `down1` / `down8` | 64 MiB down through SOCKS, 1 and 8 connections, median over `--runs` |
| `up1` | 32 MiB up, 1 connection |
| `ttfb` | 30 new connections: TCP to SOCKS + CONNECT + tunnel setup + 1 byte from the target; median and p95 |
| `conns` | 200 connections, 4 in flight, connections per second |
| `srv_cpu` / `cli_cpu` | utime+stime of the processes from `/proc/<pid>/stat` per MiB |

Every measurement is preceded by warmup: the first connection through a fresh engine is not
representative (on loopback it sometimes stalls for ~5 s — a property of the bench, not of the
engine).

## Topology

```
bench (client) ──SOCKS──▶ engine client ──REALITY/XHTTP──▶ engine server ──▶ sink
     │                        ▲   netem 25+25 ms on the tunnel port only     │
     └─────────── no delay ───┴───────────────────────────────────────────────┘
```

- The sink listens on `203.0.113.7:19000` — a TEST-NET-3 address added to `lo`. It keeps client
  routing from sending the target direct: sing-box/Unbihex send private addresses direct, and
  Xray `freedom` blocks them by default (the bench configs allow it through `finalRules`).
- The TLS "site" for REALITY is `openssl s_server` on `127.0.0.1:18446` with a self-signed
  certificate for `localhost`.
- The delay is applied by `tc`/`netem` on `lo` **only** to the tunnel ports, in both directions:
  the SOCKS handshake and the server→sink path are not delayed, otherwise TTFB lies.

## Requirements

- Linux, root (needs `tc` and an alias on `lo`).
- `curl`, `tar`, `openssl`, `python3`, `ip`, `ss`, `tc`.
- `cargo` — to build Unbihex from source (`UNBIHEX_REPO=...`).
- TUN mode needs `/dev/net/tun`; this bench does not use or test it.

## Quick start

```bash
UNBIHEX_REPO=/path/to/unbihex ./setup.sh   # pinned Xray/sing-box, keys, configs, dest and sink
./stand.py up xray_reality
./stand.py run xray_reality 0              # RTT≈0; 50 emulates 50 ms
./stand.py run xray_reality 50 --quick
./stand.py report
./teardown.sh
```

`setup.sh` is idempotent: a second run downloads and regenerates nothing. Binaries are pinned by
version and sha256 in [`versions.env`](versions.env).

## Scenarios

The list lives in [`lib/scenarios.json`](lib/scenarios.json): `xray_reality`, `xray_xhttp`,
`singbox_reality`, `unbihex_reality`, `xraycl_ubisrv`, `ubicl_xraysrv`.

To add one, extend `scenarios.json` (`ports`, `socks`, `start` — command and arguments) and, if a
new config is needed, add a template in `configs/` with `@VARIABLES@` (substituted by `setup.sh`).

## A/B: before and after a fix

Comparing old and new binaries is the only honest way to show a fix's effect:

```bash
# manually: same bench, different binaries, labels in the results
UNBIHEX_SERVER=/path/to/old/unbihex ./stand.py up xraycl_ubisrv
./stand.py run xraycl_ubisrv 0 --label before
# rebuild, restart with the new binary
./stand.py run xraycl_ubisrv 0 --label after

# automatically: A/B/A/B alternation so machine drift does not land in the difference
./stand.py ab xray_reality unbihex_reality 50 --rounds 3 --quick
```

`bins.env` (`.state/bins.env`, written by `setup.sh`) can be overridden with the `UNBIHEX_SERVER`
and `UNBIHEX_CLI` environment variables.

## Negative control

```bash
./stand.py check xray_reality
```

It verifies that traffic **stops** when the server is stopped and resumes after a restart. Without
it, a direct bypass is easy to measure by accident: once on this bench the Unbihex client sent the
target direct and the numbers looked 3x better than reality.

## Provenance

Every result in `results/*.json` records: versions and sha256 of all binaries, `git commit`,
`branch` and `dirty`, the kernel, CPU model, core count, `net.ipv4.tcp_rmem`,
`net.core.{r,w}mem_max`, netem parameters and port list, and the start time. The table printed by
`./stand.py report` shows the commit next to the numbers.

## Limitations (important)

- **Loopback MTU 65536**: segments of 27–65 KB instead of 1400. Absolute MB/s are inflated and CPU
  per MiB is understated. Only compare cells with each other.
- **No bandwidth limit and no loss**: delay only. At 8 streams and 50 ms `netem` itself becomes
  the bottleneck — do not quote those cells.
- **No TUN**: the main Unbihex mode is not measured here, and `/dev/net/tun` may be missing in a
  container. TUN changes are covered by unit tests, not by this bench.
- **Competitors out of the box**: Go binaries without `sockopt` tuning, Unbihex with its defaults.
  This compares what ships, not what is possible.
- **CPU without system time**: loopback is dominated by syscalls, so CPU differences are
  understated for everyone.
- **REALITY `dest` is local**: the cost of probing a real remote site is higher than here.

## Known traps

- `pkill -f <substring>` in this directory is dangerous: the pattern matches the script's own
  command line. The bench uses pid files in `.state/pids`.
- Socket buffers are capped by `net.core.{r,w}mem_max`; on a host with the default 212 KB the
  `SO_RCVBUF` change gives a smaller effect than on a machine with 16 MB.
- Xray `freedom` blocks private and reserved addresses by default for VLESS inbounds. The bench
  configs allow the target explicitly (`finalRules`); otherwise the tunnel "works" but no data
  flows.
