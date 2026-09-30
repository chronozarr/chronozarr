#!/usr/bin/env bash
# Upload chronozarr stores to R2 with wrangler, 4 files at a time. Re-runnable per object.
# STORE selects the prefix under each AOI (default chronozarr), e.g. STORE=chronozarr-3.
set -euo pipefail
BUCKET="${BUCKET:-tileripper-stores}"
STORE="${STORE:-chronozarr}"
ROOT=/Users/jakegearon/projects/tile-ripper/data/stores
cd "$ROOT"
# shellcheck disable=SC2016  # $1 and $f are expanded by the inner sh, not here
for aoi in "$@"; do
  find "$aoi/$STORE" -type f | sort
done | xargs -P 4 -n 1 sh -c '
  f="$1"
  if npx --no-install wrangler r2 object put "'"$BUCKET"'/$f" --file "$f" --remote --cache-control "public, max-age=31536000, immutable" > /dev/null 2>&1; then
    echo "ok $f"
  else
    echo "FAIL $f"
  fi
' _
