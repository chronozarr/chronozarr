#!/usr/bin/env bash
# Save what newcomer-N left behind, then remove the container.
#
#   scripts/trial/stop-newcomer.sh N
#
# Into <main checkout>/data/trial/newcomer-N/: bash_history, shots/ (the look screenshots) and bg/ (logs of background
# commands such as share). session.log is already there.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib.sh
. "$here/lib.sh"

[ "$#" -eq 1 ] || die "usage: stop-newcomer.sh N"
n=$1
name="newcomer-$n"
docker container inspect "$name" >/dev/null 2>&1 || die "no container $name"
out="$(trial_data_dir)/newcomer-$n"
mkdir -p "$out/shots"

if docker cp "$name:/home/scientist/.bash_history" "$out/bash_history" 2>/dev/null; then
  echo "saved $out/bash_history"
else
  echo "no shell history in $name"
fi
docker cp "$name:/home/scientist/shots/." "$out/shots/"
echo "saved $(find "$out/shots" -type f | wc -l | tr -d ' ') screenshots to $out/shots"
if docker cp "$name:/home/scientist/bg" "$out/bg" 2>/dev/null; then
  echo "saved background logs to $out/bg"
fi
echo "stopped: $(now_iso)" >>"$out/start.txt"
docker rm -f "$name" >/dev/null
echo "removed $name"
