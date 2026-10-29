#!/bin/bash
# One command: check the latest Xray / sing-box / Unbihex versions, run the
# matrix, print the results and write report.md.
#
#   ./bench.sh                          # RTT 0 and 50, full metrics
#   ./bench.sh --quick                  # download + TTFB only
#   ./bench.sh --rtt 50 --runs 5
#   ./bench.sh --scenarios xray_reality,unbihex_reality --rtt 0
#   ./bench.sh --no-control             # skip the "traffic must stop" check
set -euo pipefail
cd "$(dirname "$0")"
exec python3 stand.py bench "$@"
