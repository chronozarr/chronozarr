#!/usr/bin/env bash
# Globalping phases of the 2026-10-18 resume, one after the other (each phase reads the objects the earlier ones
# sampled, so no region is asked for an object it already holds). Output: data/fieldperf/2026-10-18/.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
out="${FIELDPERF_OUT:-$here/../../data/fieldperf/2026-10-18}"
mkdir -p "$out"
run() {
  echo "=== $(date -u +%FT%TZ) gp_collect.mjs $*"
  node "$here/gp_collect.mjs" "$@"
}

{
  run chunks --regions singapore --target 30 --max 64 --seed 20261018 --tag dc
  run meta --target 30 --max 50 --seed 20261018 --tag dc
  run plain --target 10 --tag dc
  run chunks --regions los_angeles --target 20 --max 30 --seed 20261018 --tag dc
  run chunks --regions frankfurt,sao_paulo,singapore --tags eyeball-network --target 20 --max 30 --seed 20261019 --tag eyeball
  run chunks --regions frankfurt,sao_paulo,ashburn --target 30 --max 40 --seed 20261018 --tag dc
  echo "=== $(date -u +%FT%TZ) all phases finished"
} >>"$out/gp_run_all.log" 2>&1
