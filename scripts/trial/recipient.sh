#!/usr/bin/env bash
# Open a link the way a recipient would: in a fresh machine with no data and no chronozarr installed.
#
#   scripts/trial/recipient.sh URL [look options]
#
# Runs `look` in a throwaway chronozarr-newcomer:ubuntu container, prints what it reports (first-frame time, the date
# and position the viewer shows, errors) and copies the screenshots to <main checkout>/data/trial/recipient/<time>/.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib.sh
. "$here/lib.sh"

[ "$#" -ge 1 ] || die "usage: recipient.sh URL [look options]"
image=chronozarr-newcomer:ubuntu
docker image inspect "$image" >/dev/null 2>&1 || die "no image '$image'; run scripts/trial/build-newcomer-images.sh"

stamp=$(date -u +%Y%m%dT%H%M%SZ)
out="$(trial_data_dir)/recipient/$stamp"
mkdir -p "$out"
name="recipient-$$"
docker run -d --init --name "$name" --memory 3g --memory-swap 3g "$image" >/dev/null
trap 'docker rm -f "$name" >/dev/null 2>&1' EXIT

echo "recipient: fresh $image container, no data, no chronozarr; $(now_iso)"
set +e
docker exec "${EXEC_ENV[@]}" "$name" bash -lc 'look "$@"' look "$@" 2>&1 | tee "$out/look.txt"
rc=${PIPESTATUS[0]}
set -e
docker cp "$name:/home/scientist/shots/." "$out/" >/dev/null
echo "copied to $out"
exit "$rc"
