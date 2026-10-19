#!/usr/bin/env bash
# Start the container newcomer-N: a clean machine for a scientist who has never used chronozarr.
#
#   scripts/trial/start-newcomer.sh N IMAGE DATASET_DIR
#
# IMAGE is a name such as chronozarr-newcomer:ubuntu, or just ubuntu / conda. A copy of DATASET_DIR is at
# /home/scientist/data. Records go to <main checkout>/data/trial/newcomer-N/.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib.sh
. "$here/lib.sh"

[ "$#" -eq 3 ] || die "usage: start-newcomer.sh N IMAGE DATASET_DIR"
n=$1
image=$2
dataset=$3
case "$n" in
'' | *[!0-9]*) die "N must be a number, got '$n'" ;;
esac
case "$image" in
*:*) ;;
*) image="chronozarr-newcomer:$image" ;;
esac
[ -d "$dataset" ] || die "DATASET_DIR '$dataset' is not a directory"
docker image inspect "$image" >/dev/null 2>&1 || die "no image '$image'; run scripts/trial/build-newcomer-images.sh"
name="newcomer-$n"
if docker container inspect "$name" >/dev/null 2>&1; then
  die "$name already exists; run scripts/trial/stop-newcomer.sh $n first"
fi

out="$(trial_data_dir)/newcomer-$n"
[ ! -e "$out/session.log" ] || die "$out already holds a session; use another N or move that directory"
mkdir -p "$out"

# Python and conda package hosts resolve to 0.0.0.0, so that nothing newer than the wheelhouse can be installed, even
# with an explicit --index-url. The wheelhouse itself needs no network. GitHub stays reachable (uv's Python downloads).
blocked=(pypi.org files.pythonhosted.org upload.pypi.org test.pypi.org conda.anaconda.org repo.anaconda.com anaconda.org prefix.dev)
add_host=()
for host in "${blocked[@]}"; do
  add_host+=(--add-host "$host=0.0.0.0")
done

docker run -d --init --name "$name" --memory 3g --memory-swap 3g "${add_host[@]}" "$image" >/dev/null
trap 'docker rm -f "$name" >/dev/null 2>&1' ERR
docker exec -u root "$name" mkdir /home/scientist/data
docker cp "$dataset/." "$name:/home/scientist/data/"
docker exec -u root "$name" chown -R scientist:scientist /home/scientist/data

{
  echo "started:  $(now_iso)"
  echo "name:     $name"
  echo "image:    $image ($(docker image inspect "$image" --format '{{.Id}}'))"
  echo "dataset:  $dataset ($(find "$dataset" -type f | wc -l | tr -d ' ') files, $(du -sh "$dataset" | cut -f1)), copied to /home/scientist/data"
  echo "limits:   3 GB memory, no swap beyond it; network on; PyPI and conda hosts blocked"
} | tee "$out/start.txt"
echo "run commands with: scripts/trial/x.sh $n 'CMD'"
