#!/usr/bin/env bash
# Upload chronozarr stores to R2 with wrangler, 4 files at a time. Re-runnable per object.
set -euo pipefail
BUCKET="${BUCKET:-tileripper-stores}"
ROOT=/Users/jakegearon/projects/tile-ripper/data/stores
cd "$ROOT"
for aoi in "$@"; do
  find "$aoi/chronozarr" -type f | sort
done | xargs -P 4 -n 1 sh -c '
  f="$1"
  if npx --no-install wrangler r2 object put "'"$BUCKET"'/$f" --file "$f" --remote > /dev/null 2>&1; then
    echo "ok $f"
  else
    echo "FAIL $f"
  fi
' _
