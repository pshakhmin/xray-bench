#!/bin/bash
# Stop everything the stand started and undo the host changes it made.
#   ./teardown.sh            stop processes, remove the tc rule and the IP alias
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

if [ "${1:-}" != "--keep-net" ]; then
  ip addr del "$SINK_IP/32" dev lo 2>/dev/null || true
fi

echo "stand stopped"
