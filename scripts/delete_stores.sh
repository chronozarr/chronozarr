#!/usr/bin/env bash
# Delete chronozarr stores from R2 by key, 4 at a time. Keys come from the local store tree.
set -euo pipefail
BUCKET="${BUCKET:-tileripper-stores}"
ROOT=/Users/jakegearon/projects/tile-ripper/data/stores
cd "$ROOT"
# shellcheck disable=SC2016  # $1 and $f are expanded by the inner sh, not here
for aoi in "$@"; do
  find "$aoi/chronozarr" -type f | sort
done | xargs -P 4 -n 1 sh -c '
  f="$1"
  if npx --no-install wrangler r2 object delete "'"$BUCKET"'/$f" --remote > /dev/null 2>&1; then
    echo "deleted $f"
  else
    echo "FAIL $f"
  fi
' _
