# xray-bench

**English** · [Русский](README.md)

[![bench](https://github.com/pshakhmin/xray-bench/actions/workflows/bench.yml/badge.svg)](https://github.com/pshakhmin/xray-bench/actions/workflows/bench.yml)

A reproducible bench for comparing three proxy engines — **Xray-core**, **sing-box** and
**Unbihex** — along the path "SOCKS inbound → REALITY/XHTTP → server → sink": emulated RTT,
throughput, TTFB, connection rate and CPU, with provenance recorded in every result.

The bench answers "which engine is faster under identical conditions", not "how much will a real
link give". The limitations are listed below — read them before quoting the numbers.

## Results

One bench, one run, medians. Self-pair: the client and the server of the same engine, REALITY,
loopback, 16 cores, RTT via `netem` on the tunnel port only. Base `5026c2d` (v0.2.2), 2026-10-29.

![Throughput at RTT≈0: Xray 360/806/341, sing-box 540/1306/530, Unbihex stock 437/1011/386, Unbihex + PR #18 430/1163/411 MB/s](charts/throughput-rtt0.svg)

![Single stream at RTT≈0 and 50 ms: Unbihex stock falls from 437.5 to 5.8 MB/s, the others hold 86–104](charts/throughput-rtt50.svg)

![TTFB and connection rate at RTT≈0: best TTFB is Unbihex + PR #18 (1.98 ms), best conn/s is the same build (938)](charts/connections.svg)

Exact numbers from the same runs:

| Engine (self-pair) | ↓1, RTT≈0 | ↓8, RTT≈0 | ↑1, RTT≈0 | TTFB | conn/s | ↓1, RTT 50 ms | TTFB, RTT 50 |
|---|---|---|---|---|---|---|---|
| Xray 26.10.29 | 361 MB/s | 806 MB/s | 341 MB/s | 3.95 ms | 448 | 88.6 MB/s | 154 ms |
| sing-box 1.14.4 | 540 MB/s | 1306 MB/s | 530 MB/s | 3.34 ms | 563 | 103.6 MB/s | 154 ms |
| Unbihex 0.2.2 (stock) | 437 MB/s | 1011 MB/s | 386 MB/s | 5.57 ms | 276 | 5.8 MB/s | 156 ms |
| Unbihex 0.2.2 + [PR #18](https://github.com/crystalmasta/unbihex/pull/18) | 430 MB/s | 1163 MB/s | 411 MB/s | 1.98 ms | 938 | 85.8 MB/s | 152 ms |

These are protocol-layer numbers on loopback: compare cells with each other, not with a real link
(see [Limitations](#limitations-important)). "PR #18" is the branch with the TUN, socket-buffer
and REALITY changes from [unbihex#18](https://github.com/crystalmasta/unbihex/pull/18); "stock"
is the released 0.2.2.

The charts and the table come from the same runs. `results/` is not committed, so the medians
are duplicated in [`charts/data.json`](charts/data.json) with links to the source files;
regenerate the images with `python3 charts/make_charts.py`.

The Unbihex row is about the **engine build** (engine 0.1.1: stock 0.2.2 versus the PR #18 branch).
Since 0.2.3 the Unbihex release is `unbihex-cli` plus a separate `test_server` with its own config
schema; the stand installs those by default and measures them as `unbihex_release`, so do not compare
its numbers with this table directly.

### `wan` profile (16 cores, medians of 3 runs)

Same stand, `--profile wan`: MTU 1500, GSO/TSO/GRO off, delay, 2 ms jitter, 0.1% loss. Ranges are
min–max within the run. These are the numbers CI refreshes on Pages.

| Engine (release binaries) | ↓1 MB/s, RTT≈0 | ↑1 MB/s, RTT≈0 | TTFB, RTT≈0 | conn/s | RSS server/client |
|---|---|---|---|---|---|
| Xray 26.10.29 | 132 [103–155] | 93 | 7.2 ms (p95 10.9) | 168 | 37/35 MB |
| sing-box 1.14.4 | 181 [108–182] | 168 | 7.1 ms (p95 11.5) | 379 | 61/62 MB |
| Unbihex 0.2.3 (`test_server` 0.0.1) | 95 [79–99] | 100 | 6.4 ms (p95 10.9) | 380 | 8/8 MB |

| Engine | ↓1 MB/s, RTT 50 ms | TTFB, RTT 50 ms |
|---|---|---|
| Xray 26.10.29 | 63.1 [60.4–65.6] | 155.8 ms |
| sing-box 1.14.4 | 79.6 [75.5–80.0] | 155.0 ms |
| Unbihex 0.2.3 | 49.5 [47.9–50.9] | 154.1 ms |

What it shows immediately: with real segmentation absolute numbers are three times lower than the
loopback table, `↓1` spread reaches 40% (0.1% loss plus slow start), and Unbihex and sing-box reach
three times Xray's connection rate. `↓8` is deliberately absent: with the same total payload
(64 MiB across all connections) it measures the start of eight streams rather than steady state and
comes out below `↓1` on a lossy profile. That is the next semantic fix.

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

## Link profile

By default (`--profile wan`) `lo` becomes a link that behaves like a real one: MTU 1500,
GSO/TSO/GRO off, delay rtt/2 each way, 2 ms jitter, 0.1% loss. `--profile loopback` keeps the old
behaviour (MTU 65536, offloads on, delay only); the table and charts above were taken with it.

- MTU 1500 alone changes nothing: with GSO on, segments are coalesced anyway. Direct loopback,
  256 MiB × 3 runs: 1413 MB/s (65536, GSO on) versus 602 MB/s (1500, GSO off) — that is the cost of
  real segmentation.
- `--loss PCT` and `--rate MBIT` override the profile. A rate cap deliberately flattens throughput:
  at 100 Mbit every engine saturates the link and only CPU, TTFB and conn/s can differ.
- Every result records `link` (profile, MTU, offload, delay, loss, rate), `machine` (CPU/RAM), and
  per phase `sys` (system CPU, available memory, engine RSS) and `evidence` (qdisc drops/backlog,
  tunnel socket retransmits), plus `valid` / `invalid_reasons`.

## Precision and validity

`./stand.py report --stats` prints min–max, the number of runs and the spread next to each median:
`!` marks a rejected run (errors, in-phase spread > 60%), `§` a metric whose spread exceeds 20%.

Repeatability of the same binary between sessions on this machine: 1–6% for `down1`, up to 33% for
`down8`. **Differences below ~15% are noise**; build claims on `stand.py ab` (alternating A/B) and on
metrics where the gap is a multiple of the spread.

## Topology

![Bench path: bench client → engine client → REALITY/XHTTP → engine server → sink; netem 25+25 ms on the tunnel port only, SOCKS and the path to the sink undelayed](charts/topology.svg)

- The sink listens on `203.0.113.7:19000` — a TEST-NET-3 address added to `lo`. It keeps client
  routing from sending the target direct: sing-box/Unbihex send private addresses direct, and
  Xray `freedom` blocks them by default (the bench configs allow it through `finalRules`).
- The TLS "site" for REALITY is `openssl s_server` on `127.0.0.1:18446` with a self-signed
  certificate for `localhost`.
- The delay is applied by `tc`/`netem` on `lo` **only** to the tunnel ports, in both directions:
  the SOCKS handshake and the server→sink path are not delayed, otherwise TTFB lies.

## Requirements

- Linux, root (needs `tc` and an alias on `lo`).
- `curl`, `tar`, `openssl`, `python3`, `ip`, `ss`, `tc`; `ethtool` to turn GSO/TSO/GRO
  off on `lo` (without it the `wan` profile degrades to MTU-only).
- `cargo` — to build the `bench/` harness; and for Unbihex from source (`UNBIHEX_REPO=...`).
- TUN mode needs `/dev/net/tun`; this bench does not use or test it.

## Quick start

```bash
./setup.sh                                 # release binaries by pin, keys, configs, dest and sink
./stand.py up xray_reality
./stand.py run xray_reality 0              # RTT≈0; 50 emulates 50 ms
./stand.py run xray_reality 50 --quick
./stand.py report
./teardown.sh
```

`setup.sh` is idempotent: a second run downloads and regenerates nothing. Binaries are pinned by
version and sha256 in [`versions.env`](versions.env).

Engines come as release binaries: Xray and sing-box from their official archives, Unbihex as
`unbihex-cli` plus `test_server`. To build Unbihex from source (e.g. the PR #18 branch, which has no
release): `UNBIHEX_REPO=/path/to/unbihex ./setup.sh`.

## CI and Pages

Once a week [`.github/workflows/bench.yml`](.github/workflows/bench.yml) updates the pins
(`tools/pins.py --update`, every new asset verified by sha256), runs the stand on a GitHub runner and
publishes the dashboard: **<https://pshakhmin.github.io/xray-bench/>**. Raw results of the last runs
are committed to [`ci-results/`](ci-results).

- The runner has 4 vCPU against 16 cores for the table above: CI numbers are comparable only within
  one run.
- If `netem` is unavailable on the runner, RTT 50 ms is not measured and the page omits those cells.
- Manual run: Actions → **bench** → *Run workflow*. Nothing else has to stay up: setup, the run, the
  results commit and the deploy are one workflow.

## Scenarios

The list lives in [`lib/scenarios.json`](lib/scenarios.json): `xray_reality`, `xray_xhttp`,
`singbox_reality`, `unbihex_release` (release binaries), `unbihex_reality` (source build, needs
`UNBIHEX_REPO`), `xraycl_ubisrv`, `ubicl_xraysrv`.

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
and `UNBIHEX_CLI` environment variables. The `unbihex_reality`, `xraycl_ubisrv` and `ubicl_xraysrv`
scenarios use the source build: run `UNBIHEX_REPO=/path/to/unbihex ./setup.sh` first.

## Negative control

```bash
./stand.py check xray_reality
```

It verifies that traffic **stops** when the server is stopped and resumes after a restart. Without
it, a direct bypass is easy to measure by accident: once on this bench the Unbihex client sent the
target direct and the numbers looked 3x better than reality.

## DNS

`tools/dnsd.py` is a small authoritative-ish upstream we control (UDP and TCP on the same port,
stdlib only, zone in `tools/dns-zone.json`: 200 names, A for all and AAAA for half). The DNS
scenarios point the engine's **own** resolver at it — Xray `dns.servers` + freedom
`domainStrategy: UseIP`, sing-box `dns.servers` + `domain_resolver` on the direct outbound,
Unbihex `dns.upstream` — and exchange one SOCKS CONNECT + sink ping per name:

```bash
./stand.py dns xray_reality cold 100       # 100 uncached names, per-name latency and upstream queries
./stand.py dns xray_reality warm 100       # same names again: cache must answer, 0 upstream queries
./stand.py dns singbox_reality stampede 20 # 20 parallel CONNECTs to one name: 1-2 queries, not 20
./stand.py dns singbox_reality dead        # upstream 1 is a black hole, upstream 2 is healthy
./stand.py dns xray_reality leak           # counters, then dnsd stopped: a fresh name must fail
./stand.py report
```

Options: `--delay-ms N` makes dnsd answer slowly, `--rtt N` adds netem, `--label TEXT`.
`--no-soa` runs dnsd without the negative-answer SOA, so the cache difference can be measured. Each result
records the engine and binary hashes, git commit, dnsd mode/delay, query counts per name and qtype,
latency median/p95 and whether dnsd saw any traffic. `--fail` is the black hole; `--refuse` answers
REFUSED. A quick wire check of dnsd itself:

```bash
python3 tools/dnsd.py --port 15353 --log /tmp/dnsd.jsonl --stats /tmp/dnsd.stats.json &
dig @127.0.0.1 -p 15353 n000.dns.test A
dig @127.0.0.1 -p 15353 +tcp n000.dns.test AAAA
```

`leak` is the negative control: with dnsd as the only upstream, a fresh name succeeds and dnsd's
log counts the queries; after dnsd is stopped the same CONNECT must fail. If it still succeeds, the
engine used the OS resolver or a cached answer and the DNS numbers are worthless. NODATA and
NXDOMAIN answers carry the zone SOA in AUTHORITY (RFC 2308) so the engine can negative-cache them;
`--no-soa` reproduces the old authority-less behaviour for an explicit comparison.

**Unbihex:** it uses its own resolver only when DNS is configured (or a TUN is present); otherwise it uses the OS resolver, and the scenario refuses to report numbers in that case. The default template uses `dns.upstream`; pass `--dns-servers` for the newer `dns.servers` shape. Until the engine change lands (branch `dns/all`), a run exits with `dnsd received 0 queries ... measurement is invalid` rather than printing a fake number.

## Provenance

Every result in `results/*.json` records: versions and sha256 of all binaries, `git commit`,
`branch` and `dirty`, the kernel, CPU model, core count, `net.ipv4.tcp_rmem`,
`net.core.{r,w}mem_max`, the link profile (`link`), system CPU and RAM (`sys`), qdisc and tunnel
socket counters (`evidence`), a `valid` flag with rejection reasons, and the start time. The table
printed by `./stand.py report` shows the commit next to the numbers; `--stats` adds the spread.

## Limitations (important)

- **Loopback even with the `wan` profile**: MTU, offloads and loss are emulated, but this is not a
  NIC — there is no driver queue and no hardware. Do not carry absolute MB/s over to a real link.
- **No bandwidth cap by default**: `--rate` exists, but the link is unlimited. At 8 streams and
  50 ms `netem` itself becomes the bottleneck — now visible in `evidence.qdisc`
  (drops/overlimits); do not quote those cells.
- **No TUN**: the main Unbihex mode is not measured here, and `/dev/net/tun` may be missing in a
  container. TUN changes are covered by unit tests, not by this bench.
- **Competitors out of the box**: Go binaries without `sockopt` tuning, Unbihex with its defaults.
  This compares what ships, not what is possible.
- **CPU**: besides process utime+stime the results now carry system-wide busy time
  (`sys.cpu.busy_ms`), but softirq/ksoftirqd are not attributed to a specific engine; loopback is
  dominated by syscalls.
- **REALITY `dest` is local**: the cost of probing a real remote site is higher than here.

## Known traps

- `pkill -f <substring>` in this directory is dangerous: the pattern matches the script's own
  command line. The bench uses pid files in `.state/pids`.
- Socket buffers are capped by `net.core.{r,w}mem_max`; on a host with the default 212 KB the
  `SO_RCVBUF` change gives a smaller effect than on a machine with 16 MB.
- Xray `freedom` blocks private and reserved addresses by default for VLESS inbounds. The bench
  configs allow the target explicitly (`finalRules`); otherwise the tunnel "works" but no data
  flows.
