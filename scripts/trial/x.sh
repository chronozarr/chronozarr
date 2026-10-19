#!/usr/bin/env bash
# Run a command as the scientist inside newcomer-N and keep a record.
#
#   scripts/trial/x.sh N 'CMD'              run CMD in a login bash; output streams here
#   scripts/trial/x.sh N --bg NAME 'CMD'    start CMD detached; its output goes to ~/bg/NAME.log in the container
#   scripts/trial/x.sh N --tail NAME [LINES]   show the end of ~/bg/NAME.log (default 40 lines)
#
# Every call appends the time, the command, the exit code, the duration and the full output to
# <main checkout>/data/trial/newcomer-N/session.log. Each command gets a fresh login shell with no terminal and no stdin.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib.sh
. "$here/lib.sh"

[ "$#" -ge 2 ] || die "usage: x.sh N 'CMD' | x.sh N --bg NAME 'CMD' | x.sh N --tail NAME [LINES]"
n=$1
shift
name="newcomer-$n"
log="$(trial_data_dir)/newcomer-$n/session.log"
[ -d "$(dirname "$log")" ] || die "no records directory for $name; run start-newcomer.sh first"
[ "$(docker container inspect -f '{{.State.Running}}' "$name" 2>/dev/null)" = true ] || die "$name is not running"

# logged LABEL docker-args...: run, stream and record one docker call.
logged() {
  local label=$1
  shift
  local started t0 t1 rc
  started=$(now_iso)
  t0=$(now_secs)
  printf '### %s %s\n$ %s\n' "$started" "$name" "$label" >>"$log"
  set +e
  "$@" 2>&1 | tee -a "$log"
  rc=${PIPESTATUS[0]}
  set -e
  t1=$(now_secs)
  printf '### exit=%s duration_s=%s\n\n' "$rc" "$(perl -e 'printf "%.2f", $ARGV[1] - $ARGV[0]' "$t0" "$t1")" >>"$log"
  return "$rc"
}

# The command goes into ~/.bash_history first, as it would from a terminal, then runs in this login shell.
# shellcheck disable=SC2016 # these run inside the container, where $0 and $HOME expand
run_script='printf "%s\n" "$1" >> "$HOME/.bash_history"; eval "$1"'
# shellcheck disable=SC2016 # same
bg_script='mkdir -p "$HOME/bg"
pid_file="$HOME/bg/$0.pid"
if [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
  echo "error: background job $0 is still running (pid $(cat "$pid_file"))" >&2
  exit 1
fi
printf "%s\n" "$1" >> "$HOME/.bash_history"
echo "$$" > "$pid_file"
exec > "$HOME/bg/$0.log" 2>&1
exec bash -lc "$1"'

case "$1" in
--bg)
  [ "$#" -eq 3 ] || die "usage: x.sh N --bg NAME 'CMD'"
  bg_name=$2
  cmd=$3
  # docker exec -d returns at once; the started check below reads the log a moment later.
  logged "[background $bg_name] $cmd" docker exec -d "${EXEC_ENV[@]}" "$name" bash -lc "$bg_script" "$bg_name" "$cmd"
  echo "started in the background; read it with: scripts/trial/x.sh $n --tail $bg_name"
  ;;
--tail)
  [ "$#" -ge 2 ] || die "usage: x.sh N --tail NAME [LINES]"
  logged "[tail $2]" docker exec "${EXEC_ENV[@]}" "$name" tail -n "${3:-40}" "/home/scientist/bg/$2.log"
  ;;
*)
  [ "$#" -eq 1 ] || die "usage: x.sh N 'CMD'"
  logged "$1" docker exec "${EXEC_ENV[@]}" "$name" bash -lc "$run_script" bash "$1"
  ;;
esac
