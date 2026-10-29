#!/bin/bash
# Stop everything the stand started and undo the host changes it made.
#   ./teardown.sh            stop processes, remove the tc rule, restore lo and the IP alias
#   ./teardown.sh --keep-net leave the loopback alias in place
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
cd "$HERE"
. ./versions.env

for f in .state/pids/*.pid; do
  [ -e "$f" ] || continue
  kill "$(cat "$f")" 2>/dev/null || true
  rm -f "$f"
done
python3 stand.py down --all >/dev/null 2>&1 || true

tc qdisc del dev lo root 2>/dev/null || true

# Restore the MTU/offloads stand.py saved before it applied a link profile.
if [ -f .state/link.json ]; then
  mtu=$(python3 -c 'import json; print(json.load(open(".state/link.json")).get("mtu") or "")' 2>/dev/null || true)
  if [ -n "${mtu:-}" ]; then
    ip link set dev lo mtu "$mtu" 2>/dev/null || true
  fi
  if command -v ethtool >/dev/null 2>&1; then
    read -r gso tso gro <<EOF || true
$(python3 -c 'import json; d=json.load(open(".state/link.json")); print(d.get("gso",""), d.get("tso",""), d.get("gro",""))' 2>/dev/null || true)
EOF
    ethtool -K lo gso "${gso:-}" tso "${tso:-}" gro "${gro:-}" 2>/dev/null || true
  fi
  rm -f .state/link.json
fi

if [ "${1:-}" != "--keep-net" ]; then
  ip addr del "$SINK_IP/32" dev lo 2>/dev/null || true
fi

echo "stand stopped"
