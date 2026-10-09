#!/usr/bin/env bash
# Upload chronozarr stores to R2 with wrangler, 4 files at a time. Re-runnable per object.
#
# Usage: upload_stores.sh [--trailing-ttl SECONDS] [--newer-than FILE] [--dry-run] AOI...
# STORE selects the prefix under each AOI (default chronozarr), e.g. STORE=chronozarr-3; ROOT
# overrides the local tree.
#
# Cache-Control per object (spec 8.1 for immutable objects, 8.3 for the ones an append rewrites):
#   immutable (1 year)   every chunk and shard object except those below
#   max-age=SECONDS      zarr.json files, time/c/0 and volatility/c/* (rewritten by an append), and
#                        for each cell, level and sharded variable the shard holding the last
#                        timestep (rewritten by the next append). SECONDS defaults to 300.
# Uploads run in three phases so a reader never finds metadata that describes missing data:
# chunk and shard objects, then the zarr.json files below the root and the mutable arrays, then
# the root zarr.json. A failed upload stops the run before the next phase.
# --newer-than FILE uploads only the objects modified after FILE (touch FILE before `chronozarr
# append`), which is what an append changes. --dry-run prints "<class> <key>" for every object
# it would upload and uploads nothing.
set -euo pipefail

BUCKET="${BUCKET:-chronozarr-stores}"
STORE="${STORE:-chronozarr}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="${ROOT:-$REPO_ROOT/data/stores}"
TTL=300
DRY_RUN=0
NEWER=
while [ $# -gt 0 ]; do
  case "$1" in
  --trailing-ttl)
    TTL="${2:?--trailing-ttl needs a number of seconds}"
    shift 2
    ;;
  --newer-than)
    NEWER="${2:?--newer-than needs a file}"
    [ -f "$NEWER" ] || {
      echo "--newer-than: $NEWER is not a file" >&2
      exit 2
    }
    NEWER="$(cd "$(dirname "$NEWER")" && pwd)/$(basename "$NEWER")"
    shift 2
    ;;
  --dry-run)
    DRY_RUN=1
    shift
    ;;
  --)
    shift
    break
    ;;
  -*)
    echo "unknown option $1" >&2
    exit 2
    ;;
  *) break ;;
  esac
done
if [ $# -eq 0 ]; then
  echo "usage: $0 [--trailing-ttl SECONDS] [--newer-than FILE] [--dry-run] AOI..." >&2
  exit 2
fi
case "$TTL" in
'' | *[!0-9]*)
  echo "--trailing-ttl must be a whole number of seconds, got '$TTL'" >&2
  exit 2
  ;;
esac

# classify: print "<imm|short> <path>" for every file of the store in the current directory.
classify() {
  local sharded
  sharded="$(find . -mindepth 3 -maxdepth 3 -name zarr.json -print0 |
    xargs -0 grep -l sharding_indexed 2>/dev/null |
    sed 's|^\./||; s|/zarr\.json$||' | tr '\n' ' ' || true)"
  find . -type f | sed 's|^\./||' | sort |
    awk -v dirs="$sharded" '
      BEGIN { n = split(dirs, d, " "); for (i = 1; i <= n; i++) sharded[d[i]] = 1 }
      function mutable(p) {
        return p ~ /(^|\/)zarr\.json$/ || p ~ /(^|\/)time\/c\// || p ~ /^volatility\/c\//
      }
      {
        paths[NR] = $0
        if (mutable($0)) { kind[NR] = "short"; next }
        i = index($0, "/c/")
        arr = i ? substr($0, 1, i - 1) : ""
        if (!(arr in sharded)) { kind[NR] = "imm"; next }
        rest = substr($0, i + 3)
        slash = index(rest, "/")
        ts = substr(rest, 1, slash - 1) + 0
        cell = arr SUBSEP substr(rest, slash + 1)
        kind[NR] = "chunk"; tsv[NR] = ts; key[NR] = cell
        if (!(cell in last) || ts > last[cell]) last[cell] = ts
      }
      END {
        for (j = 1; j <= NR; j++) {
          k = kind[j]
          if (k == "chunk") k = (tsv[j] == last[key[j]]) ? "short" : "imm"
          print k, paths[j]
        }
      }'
}

# upload_phase: read "<class> <key>" lines on stdin, upload each key, 4 at a time.
upload_phase() {
  # shellcheck disable=SC2016  # $1, $2 and $cc are expanded by the inner sh, not here
  xargs -P 4 -L 1 sh -c '
    if [ "$1" = imm ]; then
      cc="public, max-age=31536000, immutable"
    else
      cc="public, max-age='"$TTL"'"
    fi
    if npx --no-install wrangler r2 object put "'"$BUCKET"'/$2" --file "$2" --remote --cache-control "$cc" > /dev/null 2>&1; then
      echo "ok $2"
    else
      echo "FAIL $2"
      exit 1
    fi
  ' _
}

cd "$ROOT"
for aoi in "$@"; do
  (
    cd "$aoi/$STORE"
    listing="$(classify)"
    if [ -n "$NEWER" ]; then
      # classification needs the whole store (which shard is last); upload only what changed
      listing="$(printf '%s\n' "$listing" |
        awk 'NR == FNR { changed[$0] = 1; next } $2 in changed' \
          <(find . -type f -newer "$NEWER" | sed 's|^\./||') -)"
    fi
    prefix="$aoi/$STORE"
    phase1="$(printf '%s\n' "$listing" | awk '$2 !~ /(^|\/)zarr\.json$/ && $2 !~ /(^|\/)time\/c\// && $2 !~ /^volatility\/c\// { print $1, "'"$prefix"'/" $2 }')"
    phase2="$(printf '%s\n' "$listing" | awk '$2 != "zarr.json" && ($2 ~ /(^|\/)zarr\.json$/ || $2 ~ /(^|\/)time\/c\// || $2 ~ /^volatility\/c\//) { print $1, "'"$prefix"'/" $2 }')"
    phase3="$(printf '%s\n' "$listing" | awk '$2 == "zarr.json" { print $1, "'"$prefix"'/" $2 }')"
    if [ "$DRY_RUN" = 1 ]; then
      printf '%s\n%s\n%s\n' "$phase1" "$phase2" "$phase3" | sed '/^$/d'
      exit 0
    fi
    # xargs runs in the store directory; keys are given relative to ROOT, so cd back
    cd "$ROOT"
    export TTL BUCKET
    for phase in "$phase1" "$phase2" "$phase3"; do
      if [ -n "$phase" ]; then
        printf '%s\n' "$phase" | upload_phase
      fi
    done
  )
done
