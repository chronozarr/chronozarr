#!/usr/bin/env bash
# Build chronozarr-newcomer:ubuntu and chronozarr-newcomer:conda.
#
#   scripts/trial/build-newcomer-images.sh
#
# Two named build contexts come from the main checkout's data/trial/: wheelhouse (newcomer/build_wheelhouse.py fills it)
# and assets (the Miniforge installer, fetched here once; the Dockerfile checks its sha256).
#
# Env WHEELHOUSE (default wheelhouse: a folder name under data/trial or a path) and TAG_SUFFIX (default empty) build a
# second pair of images beside the first: WHEELHOUSE=wheelhouse-rc-0.5.0 TAG_SUFFIX=-rc gives ubuntu-rc and conda-rc.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib.sh
. "$here/lib.sh"

trial_dir=$(trial_data_dir)
wheelhouse=${WHEELHOUSE:-wheelhouse}
case "$wheelhouse" in
/*) ;;
*) wheelhouse="$trial_dir/$wheelhouse" ;;
esac
tag_suffix=${TAG_SUFFIX:-}
assets="$trial_dir/assets"
[ -f "$wheelhouse/SHA256SUMS" ] || die "no wheelhouse at $wheelhouse; run scripts/trial/newcomer/build_wheelhouse.py"

miniforge_version=$(sed -n 's/^ARG MINIFORGE_VERSION=//p' "$here/newcomer/Dockerfile")
installer="Miniforge3-$miniforge_version-Linux-aarch64.sh"
mkdir -p "$assets"
if [ ! -f "$assets/$installer" ]; then
  curl -fL --retry 5 -o "$assets/$installer.part" \
    "https://github.com/conda-forge/miniforge/releases/download/$miniforge_version/$installer"
  mv "$assets/$installer.part" "$assets/$installer"
fi

for target in ubuntu conda; do
  docker build --build-context "wheelhouse=$wheelhouse" --build-context "assets=$assets" \
    --target "$target" -t "chronozarr-newcomer:$target$tag_suffix" "$here/newcomer"
done
docker images chronozarr-newcomer
