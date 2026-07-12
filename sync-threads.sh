#!/usr/bin/env bash
# Sync s3://prod-sage-ai/archive/threads/ -> ./threads/
# Only downloads new/changed objects; never redownloads everything.
set -euo pipefail

PROFILE=prod
SRC="s3://prod-sage-ai/archive/threads/"
DEST="./threads/"

# Refresh the SSO session only if the current one is missing/expired.
# if ! aws sts get-caller-identity --profile "$PROFILE" >/dev/null 2>&1; then
#   echo "SSO session expired or missing — logging in..."
#   aws sso login --profile "$PROFILE"
# fi

echo "Syncing $SRC -> $DEST ..."
aws s3 sync "$SRC" "$DEST" --profile "$PROFILE"
echo "Done. Local files: $(find "$DEST" -type f | wc -l | tr -d ' ')"
