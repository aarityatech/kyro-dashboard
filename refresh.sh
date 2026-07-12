#!/usr/bin/env bash
# Sync threads from S3 -> rebuild dashboard_data.js -> publish to webroot/.
# Built to run hourly on EC2 (systemd timer or cron). Safe to run by hand too.
#
# AWS auth: relies on the EC2 *instance role* (no `aws sso login` on a headless
# box). Locally you can still run it if your shell has working AWS credentials.
set -euo pipefail
cd "$(dirname "$0")"

export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-ap-south-1}"
SRC="s3://prod-sage-ai/archive/threads/"
DEST="./threads/"
WEBROOT="./webroot"

# Prevent overlapping runs if a sync takes longer than the interval.
exec 9>"/tmp/kyro-refresh.lock"
if ! flock -n 9; then
  echo "$(date -Is) refresh already running — skipping"; exit 0
fi

echo "$(date -Is) [1/3] syncing $SRC -> $DEST"
aws s3 sync "$SRC" "$DEST" --only-show-errors

echo "$(date -Is) [2/3] rebuilding dashboard data"
python3 build_dashboard.py >/dev/null

echo "$(date -Is) [3/3] publishing to $WEBROOT (allowlist: dashboard files only)"
mkdir -p "$WEBROOT/assets"
# index.html + libs rarely change; copy them so code updates get picked up.
cp -f index.html "$WEBROOT/index.html"
cp -f assets/*.js "$WEBROOT/assets/"
# data changes every run — publish atomically so nginx never serves a partial file.
cp -f dashboard_data.js "$WEBROOT/.dashboard_data.js.tmp"
mv -f "$WEBROOT/.dashboard_data.js.tmp" "$WEBROOT/dashboard_data.js"
# all-users scope (lazy-loaded by the dashboard only when the scope toggle is used).
cp -f dashboard_data_all.js "$WEBROOT/.dashboard_data_all.js.tmp"
mv -f "$WEBROOT/.dashboard_data_all.js.tmp" "$WEBROOT/dashboard_data_all.js"

echo "$(date -Is) done — $(find "$DEST" -type f | wc -l | tr -d ' ') threads files local"
