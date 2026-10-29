#!/bin/bash
# Idempotent stand setup: pinned downloads, keys, certificates, rendered configs,
# loopback alias, TLS "dest" site and the sink. Run as root (tc and an IP alias).
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
cd "$HERE"
. ./versions.env

say() { printf '\033[1m%s\033[0m\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

for tool in curl tar openssl tc ip python3 ss; do
  command -v "$tool" >/dev/null || die "$tool is required"
done
[ "$(id -u)" = 0 ] || die "run as root: the stand needs tc and an IP alias"
command -v cargo >/dev/null || export PATH="$HOME/.cargo/bin:$PATH"

mkdir -p cache .state/pids .state/certs configs logs results

# ---------------------------------------------------------------- downloads (pinned)
fetch() { # url sha256 out
  local url=$1 sha=$2 out=$3
  if [ ! -f "$out" ]; then
    say "downloading $url"
    curl -fsSL -o "$out.part" "$url" || die "download failed: $url"
    mv "$out.part" "$out"
  fi
  echo "$sha  $out" | sha256sum -c --quiet - || die "checksum mismatch: $out"
}

# A cached binary is only accepted when its version output still matches the pin;
# this makes a bumped pin re-download instead of silently keeping the old build.
bin_matches() { # path needle cmd...
  local path=$1 needle=$2; shift 2
  [ -x "$path" ] || return 1
  "$path" "$@" 2>&1 | grep -qF -- "$needle"
}

if ! bin_matches cache/xray/xray "${XRAY_VERSION#v}" version; then
  rm -rf cache/xray cache/xray.zip
  fetch "$XRAY_URL" "$XRAY_SHA256" cache/xray.zip
  mkdir -p cache/xray
  python3 -c "import zipfile; zipfile.ZipFile('cache/xray.zip').extractall('cache/xray')"
  chmod +x cache/xray/xray
fi
if ! bin_matches cache/sing-box/sing-box "$SINGBOX_VERSION" version; then
  rm -rf cache/sing-box cache/sing-box.tar.gz
  fetch "$SINGBOX_URL" "$SINGBOX_SHA256" cache/sing-box.tar.gz
  tar xzf cache/sing-box.tar.gz -C cache
  mv "cache/sing-box-${SINGBOX_VERSION}-linux-amd64" cache/sing-box
fi
cache/xray/xray version | head -1
cache/sing-box/sing-box version | head -1

# ---------------------------------------------------------------- keys and certificate
if [ ! -f .state/keys.env ]; then
  cache/xray/xray x25519 > .state/keys.raw
  pvk=$(awk '/PrivateKey:/ {print $2}' .state/keys.raw)
  pbk=$(awk '/Password/ {print $3}' .state/keys.raw)
  [ -n "$pvk" ] && [ -n "$pbk" ] || die "xray x25519 produced no key pair"
  printf 'PRIVATE_KEY=%s\nPUBLIC_KEY=%s\n' "$pvk" "$pbk" > .state/keys.env
  rm -f .state/keys.raw
fi
. .state/keys.env

if [ ! -f .state/certs/cert.pem ]; then
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
    -keyout .state/certs/key.pem -out .state/certs/cert.pem -days 30 -nodes \
    -subj "/CN=$SNI" -addext "subjectAltName=DNS:$SNI,IP:127.0.0.1" 2>/dev/null
fi

# ---------------------------------------------------------------- configs from templates
render() { # template out
  python3 - "$1" "$2" <<'PY'
import os, re, sys
src, dst = sys.argv[1], sys.argv[2]
vals = dict(os.environ)
vals.setdefault("SNI", "localhost")
vals.setdefault("SHORT_ID", "")
vals.setdefault("NETWORK", "tcp")
vals.setdefault("XHTTP_SETTINGS", "")
vals.setdefault("SINK_CIDR", vals.get("SINK_IP", "203.0.113.7") + "/24")
def sub(m):
    if m.group(1) not in vals:
        sys.exit(f"template {src}: no value for @{m.group(1)}@")
    return vals[m.group(1)]
text = re.sub(r"@([A-Z0-9_]+)@", sub, open(src).read())
open(dst, "w").write(text)
PY
}

export UUID SNI SHORT_ID DEST_PORT SINK_IP PRIVATE_KEY PUBLIC_KEY

PORT=$XRAY_REALITY_PORT      render configs/xray-server-reality.json.tmpl       configs/xray-server-reality.json
PORT=$UNBIHEX_REALITY_PORT   render configs/unbihex-server-reality.json.tmpl    configs/unbihex-server-reality.json
PORT=$XRAY_XHTTP_PORT        render configs/xray-server-xhttp-reality.json.tmpl configs/xray-server-xhttp-reality.json
PORT=$SINGBOX_REALITY_PORT   render configs/singbox-server-reality.json.tmpl    configs/singbox-server-reality.json
PORT=$UNBIHEX_RELEASE_REALITY_PORT render configs/unbihex-testserver-reality.json.tmpl configs/unbihex-testserver-reality.json

SERVER_PORT=$XRAY_REALITY_PORT SOCKS_PORT=$XRAY_SOCKS \
  render configs/xray-client-reality.json.tmpl configs/xray-client-reality.json
SERVER_PORT=$UNBIHEX_REALITY_PORT SOCKS_PORT=$XRAY_SOCKS \
  render configs/xray-client-reality.json.tmpl configs/xray-client-to-unbihex.json
SERVER_PORT=$XRAY_XHTTP_PORT SOCKS_PORT=$XRAY_SOCKS NETWORK=xhttp \
  XHTTP_SETTINGS=' ,
        "xhttpSettings": { "path": "/x", "mode": "auto" }' \
  render configs/xray-client-reality.json.tmpl configs/xray-client-xhttp-reality.json
SERVER_PORT=$SINGBOX_REALITY_PORT SOCKS_PORT=$SINGBOX_SOCKS \
  render configs/singbox-client-reality.json.tmpl configs/singbox-client-reality.json

# DNS scenarios (stand.py dns) re-render these with the real dnsd ports; the
# defaults here keep the templates exercised by setup.sh.
DNS_SERVERS_XRAY='{"address":"127.0.0.1","port":15353}'
DNS_SERVERS_SINGBOX='{"tag":"dnsd0","type":"udp","server":"127.0.0.1","server_port":15353}'
PORT=$XRAY_REALITY_PORT DNS_SERVERS="$DNS_SERVERS_XRAY" \
  render configs/xray-server-reality-dns.json.tmpl configs/xray-server-reality-dns.json
PORT=$XRAY_XHTTP_PORT DNS_SERVERS="$DNS_SERVERS_XRAY" \
  render configs/xray-server-xhttp-reality-dns.json.tmpl configs/xray-server-xhttp-reality-dns.json
PORT=$SINGBOX_REALITY_PORT DNS_SERVERS="$DNS_SERVERS_SINGBOX" DNS_FIRST=dnsd0 \
  render configs/singbox-server-reality-dns.json.tmpl configs/singbox-server-reality-dns.json
PORT=$UNBIHEX_REALITY_PORT DNS_UPSTREAMS='"127.0.0.1:15353"' \
  render configs/unbihex-server-reality-dns.json.tmpl configs/unbihex-server-reality-dns.json
PORT=$UNBIHEX_REALITY_PORT DNS_SERVERS='{"address":"127.0.0.1","port":15353,"domains":["dns.test"]}' \
  render configs/unbihex-server-reality-dns-servers.json.tmpl configs/unbihex-server-reality-dns-servers.json

# Every generated config must be valid; a broken stand fails here, not mid-measurement.
cache/xray/xray run -test -c configs/xray-server-reality.json       >/dev/null 2>&1 || die "xray-server-reality.json is invalid"
cache/xray/xray run -test -c configs/xray-server-xhttp-reality.json >/dev/null 2>&1 || die "xray-server-xhttp-reality.json is invalid"
cache/xray/xray run -test -c configs/xray-client-reality.json       >/dev/null 2>&1 || die "xray-client-reality.json is invalid"
cache/xray/xray run -test -c configs/xray-client-xhttp-reality.json >/dev/null 2>&1 || die "xray-client-xhttp-reality.json is invalid"
cache/xray/xray run -test -c configs/xray-server-reality-dns.json      >/dev/null 2>&1 || die "xray-server-reality-dns.json is invalid"
cache/xray/xray run -test -c configs/xray-server-xhttp-reality-dns.json >/dev/null 2>&1 || die "xray-server-xhttp-reality-dns.json is invalid"
cache/sing-box/sing-box check -c configs/singbox-server-reality.json 2>/dev/null || say "warning: sing-box server config rejected"
cache/sing-box/sing-box check -c configs/singbox-client-reality.json 2>/dev/null || say "warning: sing-box client config rejected"
cache/sing-box/sing-box check -c configs/singbox-server-reality-dns.json 2>/dev/null || say "warning: sing-box DNS server config rejected"

# ---------------------------------------------------------------- loopback alias, dest, sink
ip addr add "$SINK_IP/32" dev lo 2>/dev/null || true

start_bg() { # name cmd...
  local name=$1; shift
  if [ -f ".state/pids/$name.pid" ] && kill -0 "$(cat ".state/pids/$name.pid")" 2>/dev/null; then
    return 0
  fi
  setsid "$@" >"logs/$name.log" 2>&1 </dev/null &
  echo $! > ".state/pids/$name.pid"
}

if [ ! -x bench/target/release/bench ]; then
  say "building the benchmark client"
  (cd bench && cargo build --release --quiet)
fi

start_bg dest openssl s_server -accept "127.0.0.1:$DEST_PORT" \
  -cert .state/certs/cert.pem -key .state/certs/key.pem -tls1_3 -alpn h2,http/1.1 -www -quiet
start_bg sink ./bench/target/release/bench sink --listen "$SINK_IP:$SINK_PORT"

# ---------------------------------------------------------------- unbihex source build or release binaries
# Set UNBIHEX_REPO to build from a checkout (e.g. the PR #18 branch, which has no
# release); otherwise install the pinned upstream release binaries.
if [ -n "${UNBIHEX_REPO:-}" ]; then
  command -v cargo >/dev/null || die "cargo is required to build unbihex"
  say "building unbihex from $UNBIHEX_REPO"
  (cd "$UNBIHEX_REPO/unbihex-server" && cargo build --release --quiet)
  (cd "$UNBIHEX_REPO/unbihex-client" && cargo build --release --quiet -p unbihex-cli)
  cat > .state/bins.env <<EOF
UNBIHEX_REPO=$UNBIHEX_REPO
UNBIHEX_SERVER=$UNBIHEX_REPO/unbihex-server/target/release/unbihex
UNBIHEX_CLI=$UNBIHEX_REPO/unbihex-client/target/release/unbihex-cli
EOF
else
  say "installing unbihex release $UNBIHEX_VERSION"
  if ! bin_matches cache/unbihex-cli/unbihex-cli "${UNBIHEX_VERSION#v}" version; then
    rm -rf cache/unbihex-cli "cache/$UNBIHEX_CLI_TGZ"
    fetch "$UNBIHEX_CLI_URL" "$UNBIHEX_CLI_SHA256" "cache/$UNBIHEX_CLI_TGZ"
    mkdir -p cache/unbihex-cli
    tar xzf "cache/$UNBIHEX_CLI_TGZ" -C cache/unbihex-cli --strip-components=1
    chmod +x cache/unbihex-cli/unbihex-cli
  fi
  if ! bin_matches cache/unbihex-server/test_server "$UNBIHEX_SERVER_ASSET_VERSION" version; then
    rm -rf cache/unbihex-server "cache/$UNBIHEX_SERVER_TGZ"
    fetch "$UNBIHEX_SERVER_URL" "$UNBIHEX_SERVER_SHA256" "cache/$UNBIHEX_SERVER_TGZ"
    mkdir -p cache/unbihex-server
    tar xzf "cache/$UNBIHEX_SERVER_TGZ" -C cache/unbihex-server --strip-components=1
    chmod +x cache/unbihex-server/test_server
  fi
  cache/unbihex-cli/unbihex-cli version | head -1
  cache/unbihex-server/test_server version | head -1
  cache/unbihex-server/test_server check -c configs/unbihex-testserver-reality.json \
    || die "unbihex-testserver-reality.json is invalid"
  cat > .state/bins.env <<EOF
UNBIHEX_SERVER_RELEASE=$HERE/cache/unbihex-server/test_server
UNBIHEX_CLI_RELEASE=$HERE/cache/unbihex-cli/unbihex-cli
EOF
fi
[ -f .state/bins.env ] || : > .state/bins.env
. .state/bins.env

# The release client is used for every config dir when present; the source
# build otherwise. This keeps unbihex_reality/source scenarios on UNBIHEX_*.
UB_CLI="${UNBIHEX_CLI_RELEASE:-${UNBIHEX_CLI:-}}"
[ -n "$UB_CLI" ] || die "no unbihex CLI in .state/bins.env"

if ! "$UB_CLI" --dir .state/ubcli list 2>/dev/null | grep -q "xray-reality"; then
  "$UB_CLI" --dir .state/ubcli add \
    "vless://$UUID@127.0.0.1:$XRAY_REALITY_PORT?security=reality&sni=$SNI&pbk=$PUBLIC_KEY&sid=$SHORT_ID&fp=chrome&type=tcp#xray-reality" >/dev/null
fi
"$UB_CLI" --dir .state/ubcli select 1 >/dev/null
if ! "$UB_CLI" --dir .state/ubcli-ubi list 2>/dev/null | grep -q "unbihex-reality"; then
  "$UB_CLI" --dir .state/ubcli-ubi add \
    "vless://$UUID@127.0.0.1:$UNBIHEX_REALITY_PORT?security=reality&sni=$SNI&pbk=$PUBLIC_KEY&sid=$SHORT_ID&fp=chrome&type=tcp#unbihex-reality" >/dev/null
fi
"$UB_CLI" --dir .state/ubcli-ubi select 1 >/dev/null
if [ -n "${UNBIHEX_CLI_RELEASE:-}" ]; then
  if ! "$UNBIHEX_CLI_RELEASE" --dir .state/ubcli-rel list 2>/dev/null | grep -q "unbihex-release"; then
    "$UNBIHEX_CLI_RELEASE" --dir .state/ubcli-rel add \
      "vless://$UUID@127.0.0.1:$UNBIHEX_RELEASE_REALITY_PORT?security=reality&sni=$SNI&pbk=$PUBLIC_KEY&sid=$SHORT_ID&fp=chrome&type=tcp#unbihex-release" >/dev/null
  fi
  "$UNBIHEX_CLI_RELEASE" --dir .state/ubcli-rel select 1 >/dev/null
fi

# ---------------------------------------------------------------- self-check
sleep 1
ss -lnt | grep -q ":$DEST_PORT " || die "dest site is not listening on $DEST_PORT"
ss -lnt | grep -q "$SINK_IP:$SINK_PORT" || die "sink is not listening on $SINK_IP:$SINK_PORT"
say "stand is up: dest 127.0.0.1:$DEST_PORT, sink $SINK_IP:$SINK_PORT"
say "next: ./stand.py up xray_reality && ./stand.py run xray_reality 0"
