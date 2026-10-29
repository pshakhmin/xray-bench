# bench

Small standalone TCP/SOCKS5 benchmark used to compare proxy engines (Xray-core,
sing-box, unbihex) through their SOCKS5 inbounds.

It contains both sides:

* `bench sink` — the server that generates deterministic download traffic,
  consumes upload traffic, and answers ping connections.
* `bench run` / `bench ttfb` / `bench conns` — the clients. They normally go
  through a SOCKS5 proxy, or straight to the sink with `--direct`.

Only `tokio` is used (no serde, no clap). All output on stdout is JSON lines;
diagnostics and help go to stderr.

## Build

```sh
export PATH="$HOME/.cargo/bin:$PATH"
cargo build --release
```

The binary is `target/release/bench`.

## Sink wire protocol

The client sends one mode byte, then:

| mode | name | behaviour |
|------|------|-----------|
| `0` | download | sink sends a 4-byte big-endian length (`u32::MAX`), then that many payload bytes, then closes |
| `1` | upload | client sends a 4-byte big-endian length `N`, then `N` bytes; sink reads exactly `N`, replies `0x01`, closes |
| `2` | ping | sink immediately sends `0x01` and keeps the connection open |

Download clients normally read only their assigned share and close early. The
sink stops when the peer goes away. Payload bytes are generated locally as a
repeating `0..=255` sequence, so compression cannot affect results.

## Usage

### Start the sink

```sh
./target/release/bench sink --listen 127.0.0.1:19000
```

### Bulk throughput

```sh
./target/release/bench run \
  --proxy 127.0.0.1:1080 \
  --target 127.0.0.1:19000 \
  --mode down --mib 256 --conns 8 --runs 5
```

For every run the client opens all `--conns` SOCKS5 connections first, starts
the timer only after all CONNECTs succeeded, splits `--mib` MiB evenly across
the connections, transfers, and stops when all connections are done.

JSON output per run:

```json
{"run":1,"mib":256,"conns":8,"seconds":0.421123,"mbps":637.901}
```

Final summary:

```json
{"event":"summary","mode":"down","median_mbps":640.112,"min_mbps":611.234,"max_mbps":651.987}
```

`MB` is `1024 * 1024` bytes; `mbps` is bytes / 1e6 / seconds.

Upload mode:

```sh
./target/release/bench run --proxy 127.0.0.1:1080 --target 127.0.0.1:19000 \
  --mode up --mib 64 --conns 4 --runs 3
```

### TTFB (connection setup through the tunnel)

```sh
./target/release/bench ttfb \
  --proxy 127.0.0.1:1080 \
  --target 127.0.0.1:19000 \
  --runs 20
```

Each run measures from `t0` (before the TCP connect to the proxy) through the
SOCKS5 CONNECT and the sink's ping reply, stopping at the first payload byte.
A new outbound is therefore created for every sample.

Per-run output:

```json
{"run":1,"ms":0.412}
```

Final summary:

```json
{"event":"summary","median_ms":0.398,"p95_ms":0.612,"runs":20,"errors":0}
```

### Connection rate

```sh
./target/release/bench conns \
  --proxy 127.0.0.1:1080 \
  --target 127.0.0.1:19000 \
  --count 1000 --par 32
```

Opens `--count` connections, at most `--par` in flight. Each one does SOCKS5
CONNECT, ping, reads one byte, and closes.

```json
{"event":"conns","count":1000,"par":32,"success":1000,"errors":0,"seconds":0.812345,"conns_per_sec":1231.234}
```

### Direct / self-test

`run`, `ttfb`, and `conns` accept `--direct`, which skips SOCKS5 and connects
straight to `--target`. This is how the tool tests itself without a proxy:

```sh
./target/release/bench run --direct --target 127.0.0.1:19000 \
  --mode down --mib 64 --conns 4 --runs 3
./target/release/bench run --direct --target 127.0.0.1:19000 \
  --mode up --mib 64 --conns 4 --runs 3
./target/release/bench ttfb --direct --target 127.0.0.1:19000 --runs 20
./target/release/bench conns --direct --target 127.0.0.1:19000 \
  --count 1000 --par 32
```

## Error policy

Every connect/read/write is bounded by a 30-second timeout. Individual
connection failures are counted and logged to stderr; the operation fails with
a non-zero exit status if more than 10% of connections fail.

SOCKS5 is no-auth, `ATYP=IPv4` only. The full CONNECT reply (including the
bound address and port) is always consumed.

## Defaults

| command | defaults |
|---------|----------|
| `run` | `--mode down --mib 64 --conns 1 --runs 3` |
| `ttfb` | `--runs 10` |
| `conns` | `--count 100 --par 10` |

`--proxy` is required unless `--direct` is used. Run `bench --help` for the
hand-rolled help text.
