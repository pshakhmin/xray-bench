# xray-bench

**English** · [Русский](README.md)

[![bench](https://github.com/pshakhmin/xray-bench/actions/workflows/bench.yml/badge.svg)](https://github.com/pshakhmin/xray-bench/actions/workflows/bench.yml)

A reproducible bench for comparing three proxy engines — **Xray-core**, **sing-box** and
**Unbihex** — along the path "SOCKS inbound → REALITY/XHTTP → server → sink": emulated RTT,
throughput, TTFB, connection rate and CPU, with provenance recorded in every result.

The bench answers "which engine is faster under identical conditions", not "how much will a real
link give". The limitations are listed below — read them before quoting the numbers.

## Results

The benchmark is a GitHub Actions runner (`ubuntu-latest`, 4 vCPU) with the `wan` profile: MTU 1500,
GSO/TSO/GRO off, 0.1% loss, delay via `netem` on the tunnel port only. Below is the latest run;
fresh numbers, charts and provenance are on **<https://pshakhmin.github.io/xray-bench/>**.

| Engine (release binaries) | ↓1 MB/s | ↓8 MB/s | ↑1 MB/s | TTFB | conn/s | ↓1, RTT 50 ms | TTFB, RTT 50 |
|---|---|---|---|---|---|---|---|
| Xray 26.10.29 | 164 [146–199] | 204 | 152 | 4.3 ms | 431 | 37.8 | 157 ms |
| sing-box 1.14.4 | 213 [182–220] | 307 | 170 | 4.3 ms | 293 | 45.2 | 157 ms |
| Unbihex 0.2.3 (`test_server` 0.0.1) | 89 [86–92] | 202 | 89 | 2.3 ms | 173 | 2.0 | 154 ms |

Run of 2026-10-30, commit `0720e82`; raw JSON lives in [`ci-results/`](ci-results) and the page is
rebuilt by `python3 site/build.py`. Medians of three runs, min–max in brackets. The runner has
4 vCPU: compare cells only within one run, never against other hardware.

Unbihex falls to 2 MB/s at 50 ms in this run. The cause is known: the engine asked the kernel for a
4 MiB `SO_RCVBUF`, and an explicit size turns receive-buffer autotuning off. The `rcv_space` estimate
then collapses from megabytes to tens of kilobytes, and a 51 KB window at 50 ms is exactly 1 MB/s.
The table is left as it is — that is the raw output of the run, before the fix. The fix (default "leave
the sockets alone") and the bulk TLS read are in PR #20 and the one after it. TTFB in that column
is simply the configured RTT (~154–157 ms) for every engine — a check that the delay is applied, not
an engine difference.

### Reference run: 16 cores, wan

The `wan` profile: MTU 1500, offloads off, no loss, 16 cores, released binaries (Unbihex 0.2.3 —
`test_server` and `unbihex-cli` from `main`). The run clock starts before the connections are
established: otherwise an engine that brings the tunnel up first and only then answers SOCKS hides
its setup inside the handshake, while an engine that answers at once pays for it in the measurement.
How much went into the setup is visible as `connect_seconds` in every result.

![Throughput at RTT≈0: Xray 181/544/194, sing-box 240/676/232, Unbihex 254/671/247 MB/s](charts/throughput-rtt0.svg)

![Single stream at RTT≈0 and 50 ms: Unbihex 253.6 → 54.9 MB/s, Xray 180.9 → 67.8, sing-box 239.5 → 71.5](charts/throughput-rtt50.svg)

![TTFB and connection rate at RTT≈0: Unbihex has the best median TTFB (2.82 ms) and the highest connection rate (890 conn/s); Xray sits at 3.46 ms and 540](charts/connections.svg)

| Engine (self-pair) | ↓1, RTT≈0 | ↓8, RTT≈0 | ↑1, RTT≈0 | TTFB | conn/s | ↓1, RTT 50 ms | TTFB, RTT 50 |
|---|---|---|---|---|---|---|---|
| Xray 26.10.29 | 181 MB/s | 544 MB/s | 194 MB/s | 3.46 ms | 540 | 67.8 MB/s | 155 ms |
| sing-box 1.14.4 | 240 MB/s | 676 MB/s | 232 MB/s | 3.46 ms | 635 | 71.5 MB/s | 156 ms |
| Unbihex 0.2.3 | 254 MB/s | 671 MB/s | 247 MB/s | 2.82 ms | 890 | 54.9 MB/s | 154 ms |

These are protocol-layer numbers on a shaped loopback path: compare cells with each other, not with
a real link (see [Limitations](#limitations-important)). At 50 ms Unbihex 0.2.3 trails both — the
release still asks for a 4 MiB `SO_RCVBUF`. On the branch with the fix (default "leave the sockets
alone" plus the bulk TLS read) the same runs give 305/937/298 MB/s at RTT≈0 and 71.2 MB/s at
RTT 50 ms; the table will be refreshed once that is merged.

The charts and the table come from the same runs. `results/` is not committed, so the medians are
duplicated in [`charts/data.json`](charts/data.json) with links to the source files; regenerate the
charts with `python3 charts/make_charts.py` (`--lang ru` for the Russian versions in `charts/ru/`).

## Reference run: 16 cores, loopback (historical)

The first run of the stand, before the `wan` profile existed: MTU 65536, offloads on, no loss,
16 cores, Unbihex 0.2.2. Do not compare it with the CI numbers above.

![Throughput at RTT≈0: Xray 360/806/341, sing-box 540/1306/530, Unbihex 437/1011/386 MB/s](charts/throughput-rtt0.svg)

![Single stream at RTT≈0 and 50 ms: Unbihex falls from 437.5 to 5.8 MB/s while Xray holds 88.6 and sing-box 103.6](charts/throughput-rtt50.svg)

![TTFB and connection rate at RTT≈0: best TTFB is sing-box at 3.34 ms and the best conn/s is the same engine (563); Unbihex has the worst p95 (11.59 ms) and the lowest conn/s (276)](charts/connections.svg)

| Engine (self-pair) | ↓1, RTT≈0 | ↓8, RTT≈0 | ↑1, RTT≈0 | TTFB | conn/s | ↓1, RTT 50 ms | TTFB, RTT 50 |
|---|---|---|---|---|---|---|---|
| Xray 26.10.29 | 361 MB/s | 806 MB/s | 341 MB/s | 3.95 ms | 448 | 88.6 MB/s | 154 ms |
| sing-box 1.14.4 | 540 MB/s | 1306 MB/s | 530 MB/s | 3.34 ms | 563 | 103.6 MB/s | 154 ms |
| Unbihex 0.2.2 | 437 MB/s | 1011 MB/s | 386 MB/s | 5.57 ms | 276 | 5.8 MB/s | 156 ms |

These are protocol-layer numbers on loopback: compare cells with each other, not with a real link
(see [Limitations](#limitations-important)). Unbihex here is the released 0.2.2; since 0.2.3 the
release is `unbihex-cli` plus a separate `test_server` with its own config schema, which the stand
installs by default and measures as `unbihex_release` — do not compare those numbers with this
table directly.

The charts and the table come from the same runs. `results/` is not committed, so the medians are
duplicated in [`charts/data.json`](charts/data.json) with links to the source files; regenerate the
images with `python3 charts/make_charts.py` (Russian variants: `--lang ru`, written to
`charts/ru/` and used by the Russian README).

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
GSO/TSO/GRO off, delay rtt/2 each way, 0.1% loss, and up to 2 ms jitter when the delay is
non-zero (jitter on a 0 ms link is pure reordering and pins cwnd at the initial window). `--profile loopback` keeps the old
behaviour (MTU 65536, offloads on, delay only); the reference run below was taken with it.

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

`bench --retry-invalid N` re-measures a cell while it stays invalid and keeps the **first valid**
attempt (not the best one). CI uses 2 retries at RTT≈0 and 3 at 50 ms — otherwise the median of three
identically collapsed runs reads like an engine result.

Repeatability of the same binary between sessions: 1–6% for `down1`, up to 33% for `down8` on the
16-core host; on the CI runner the spread is visible in `report --stats` and on the page. **Differences below ~15% are noise**; build claims on `stand.py ab` (alternating A/B) and on
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
`unbihex-cli` plus `test_server`. For a build that is not a release (your own branch or a local
patch) there is an escape hatch: `UNBIHEX_REPO=/path/to/unbihex ./setup.sh` builds the engine from
source.

## CI and Pages

CI is the source of truth. Once a week [`.github/workflows/bench.yml`](.github/workflows/bench.yml) updates the pins
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
`UNBIHEX_REPO`), `xraycl_ubisrv`, `ubicl_xraysrv` (our client → Xray server), `ubicl_xhttp` (the
same over XHTTP) and `ubicl_branch_reality` / `ubicl_branch_xhttp` (a branch build, needs
`UNBIHEX_CLI_BRANCH`). One server, several clients, so the clients compare against each other.

To add one, extend `scenarios.json` (`ports`, `socks`, `start` — command and arguments) and, if a
new config is needed, add a template in `configs/` with `@VARIABLES@` (substituted by `setup.sh`).

## Clients on one server

`xray_xhttp` and `ubicl_xhttp` point at the same Xray server, as do `xray_reality` and
`ubicl_xraysrv` — this compares the clients, not the servers. `wan` profile, no loss, RTT≈0, two
runs of 5 measurements per cell:

| client | ↓1 | ↓8 | ↑1 | TTFB | conn/s |
|---|---|---|---|---|---|
| Xray 26.10.29 | 107.5 | 284–356 | 158–163 | 1.0–1.1 ms | 1394–1564 |
| Unbihex 0.2.3 | 119–122 | 773–805 | 200–204 | 3.6–6.3 ms | 500–515 |
| Unbihex, branch | 121–127 | 383–430 | 206–226 | 0.8 ms | 1476–1542 |

Over XHTTP the 0.2.3 release moves more data but dials a tunnel for every proxied connection: its
defaults had `xmux.maxConcurrency: 1`, while Xray does not limit sessions at all. Hence 500 conn/s
against 1400–1800 and a TTFB 3–6 times higher — XHTTP behaved like plain TCP. The fix (`xhttp:
share pooled connections by default`, still on a branch) makes sessions share a pool of four
connections. The price is ↓8 at RTT≈0: eight separate TCP windows carried 773–805, the shared pool
carries 383–430. The client is still ahead of Xray, and connection setup no longer costs a
handshake.

At 50 ms the picture is even: ↓1 74 against 73, TTFB 52.1 against 52.3, conn/s 75 against 75. Do not
compare ↓8 at 50 ms — netem itself is the bottleneck there, which is why the reference table keeps
only ↓1 at 50 ms.

```
./stand.py bench --scenarios xray_xhttp,ubicl_xhttp,ubicl_branch_xhttp \
    --rtt 0 --runs 5 --profile wan --loss 0
```

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


### Results (loopback, medians, dnsd with zero delay)

| Engine | cold, 20 names | warm, same names | stampede, 20 connections to one name | first upstream is a black hole |
|---|---|---|---|---|
| Xray 26.10.29 | 4.9 ms, 40 queries (A+AAAA) | 0 upstream queries | 2 queries, 21.5 ms | works, but **8.0 s** per name |
| sing-box 1.14.4 | 4.4 ms, 40 queries | 0 queries | 2 queries, 25.7 ms | **no failover, connections fail** |
| Unbihex (`dns/all` branch) | 3.8 ms, 40 queries | 0 queries | 2 queries, 6.7 ms | 204 ms per name |

Unbihex is measured through `test_server` (its server moved out of the upstream repository) and the
server side resolves, exactly like the direct/freedom outbound does for Xray and sing-box. Released
0.2.2 cannot be measured here: without a TUN it ignores `dns.upstream` and uses the system resolver,
so the scenario refuses to print numbers instead of showing zero queries.

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
