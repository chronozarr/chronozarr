#!/usr/bin/env bash
# Upload chronozarr stores to R2 with wrangler, 4 files at a time. Idempotent per object.
set -euo pipefail
BUCKET="${BUCKET:-tileripper-stores}"
ROOT=/Users/jakegearon/projects/tile-ripper/data/stores
cd "$ROOT"
for aoi in "$@"; do
  find "$aoi/chronozarr" -type f | sort
done | xargs -P 4 -I{} sh -c 'npx --no-install wrangler r2 object put "'"$BUCKET"'/{}" --file "{}" --remote >/dev/null 2>&1 && echo "ok {}" || echo "FAIL {}"'
