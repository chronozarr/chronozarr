#!/usr/bin/env bash
# Run one `bench.py run` inside a resource-limited container.
#
#   constrained.sh --cpus 2 --memory 3g [--nofile 64] [--env KEY=VALUE]... [--tag NAME] \
#       -- --workload lab-ucayali-x2 --config auto --lab 0,0,0 --rep 0
#
# The repo is mounted read-only at /repo and data/bench read-write on top of it, so results land
# in data/bench/results. The bench starts its own lab server on localhost inside the container,
# so its memory and CPU count against the container's limits. Swap is off (memory-swap equals
# memory), so a run over the limit is killed (exit 137) instead of swapping. A killed run has no
# record from the bench, so a failure record is appended to the results file here. One sidecar
# JSON per run (container limits, exit code, OOM flag, cgroup memory peak and CPU throttling)
# goes to bench/s2-ingest/runs/.
set -euo pipefail

IMAGE=chronozarr-s2-bench
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"

cpus=""
memory=""
nofile=""
tag="run"
script=/repo/examples/sentinel2_pc/bench.py
envs=()
while [[ $# -gt 0 ]]; do
  case "$1" in
  --cpus)
    cpus="$2"
    shift 2
    ;;
  --memory)
    memory="$2"
    shift 2
    ;;
  --nofile)
    nofile="$2"
    shift 2
    ;;
  --script)
    script="$2"
    shift 2
    ;;
  --env)
    envs+=(-e "$2")
    shift 2
    ;;
  --tag)
    tag="$2"
    shift 2
    ;;
  --)
    shift
    break
    ;;
  *)
    echo "unknown option: $1 (bench arguments go after --)" >&2
    exit 2
    ;;
  esac
done
if [[ -z "$cpus" || -z "$memory" || $# -eq 0 ]]; then
  echo "usage: constrained.sh --cpus N --memory SIZE [--nofile N] [--env K=V] [--tag T] -- BENCH_RUN_ARGS" >&2
  exit 2
fi

if [[ -n "$(docker ps -q)" ]]; then
  echo "another container is running; one at a time" >&2
  exit 2
fi

name="s2bench-$(date -u +%Y%m%dT%H%M%S)-$$"
out="$(mktemp)"
err="$(mktemp)"

limits=(--cpus "$cpus" --memory "$memory" --memory-swap "$memory")
if [[ -n "$nofile" ]]; then
  limits+=(--ulimit "nofile=$nofile:$nofile")
fi

# Runs in the container: the bench, then the cgroup counters that cover the bench and its lab
# server together. The exit code of the bench is passed on.
# shellcheck disable=SC2016  # expanded by sh inside the container, not here
inner='
if [ "${SCRIPT##*/}" = bench.py ]; then set -- run "$@"; fi
/opt/venv/bin/python "$SCRIPT" "$@"
rc=$?
for f in memory.peak memory.max memory.events cpu.max cpu.stat; do
  if [ -r "/sys/fs/cgroup/$f" ]; then
    sed "s|^|CGROUP $f |" "/sys/fs/cgroup/$f" >&2
  fi
done
exit $rc
'

set +e
docker run --name "$name" "${limits[@]}" \
  -e "SCRIPT=$script" -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0=/repo \
  ${envs[@]+"${envs[@]}"} \
  -v "$repo:/repo:ro" -v "$repo/data/bench:/repo/data/bench" \
  "$IMAGE" sh -c "$inner" sh "$@" >"$out" 2>"$err"
rc=$?
set -e

oom="$(docker inspect -f '{{.State.OOMKilled}}' "$name")"
docker rm "$name" >/dev/null

(cd "$repo" && uv run python "$here/container_record.py" \
  --tag "$tag" --exit-code "$rc" --oom "$oom" \
  --cpus "$cpus" --memory "$memory" --nofile "${nofile:-}" \
  --stdout "$out" --stderr "$err" --runs-dir "$here/runs" -- "$@")
rm -f "$out" "$err"
exit "$rc"
