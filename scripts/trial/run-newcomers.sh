#!/usr/bin/env bash
# Run the simulated newcomer sessions listed in a TSV, a few at a time.
#
#   scripts/trial/run-newcomers.sh SESSIONS_TSV CORPUS_DIR WORK_ROOT [MAX_PARALLEL]
#
# Each TSV line: N, image, dataset path under CORPUS_DIR, role, data note (tab-separated). Every session runs through
# run-codex-newcomer.sh; its console output goes to <main checkout>/data/trial/newcomer-N.console.txt.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib.sh
. "$here/lib.sh"

[ "$#" -ge 3 ] || die "usage: run-newcomers.sh SESSIONS_TSV CORPUS_DIR WORK_ROOT [MAX_PARALLEL]"
sessions=$1
corpus=$2
work_root=$3
max_parallel=${4:-4}
records=$(trial_data_dir)

while IFS=$'\t' read -r n image dataset role note; do
  [ -n "$n" ] || continue
  # bash 3.2 on macOS has no `wait -n`; poll the running jobs instead.
  while [ "$(jobs -rp | wc -l | tr -d ' ')" -ge "$max_parallel" ]; do
    sleep 5
  done
  echo "$(now_iso) start newcomer-$n ($image, $dataset)"
  "$here/run-codex-newcomer.sh" "$n" "$image" "$corpus/$dataset" "$role" "$note" "$work_root" \
    >"$records/newcomer-$n.console.txt" 2>&1 &
  sleep 20
done <"$sessions"
wait
echo "$(now_iso) all sessions ended"
