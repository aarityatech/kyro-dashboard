#!/usr/bin/env bash
# Serve the Kyro analytics dashboard locally and open it in the browser.
# Usage: ./run.sh [port]   (default port 8000)
set -euo pipefail
cd "$(dirname "$0")"

PORT="8004"

# Rebuild data if the ETL is newer than the generated file (or it's missing).
if [ ! -f dashboard_data.js ] || [ build_dashboard.py -nt dashboard_data.js ]; then
  echo "Building dashboard data..."
  python3 build_dashboard.py
fi

URL="http://0.0.0.0:${PORT}"
echo "Kyro Analytics dashboard  ->  ${URL}"
echo "(Ctrl+C to stop)"
# open the browser on macOS; ignore if not available
( sleep 1; command -v open >/dev/null 2>&1 && open "${URL}" ) >/dev/null 2>&1 &

exec python3 -m http.server "${PORT}"
