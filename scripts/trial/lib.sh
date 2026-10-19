# shellcheck shell=bash
# Shared by the newcomer scripts (sourced, not run).

die() {
  echo "error: $*" >&2
  exit 1
}

# data/trial of the main checkout, wherever this script runs from (a worktree has no data/ of its own).
trial_data_dir() {
  local git_common
  git_common=$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --path-format=absolute --git-common-dir)
  echo "$(dirname "$git_common")/data/trial"
}

now_iso() {
  date -u +%Y-%m-%dT%H:%M:%SZ
}

# Seconds with a fraction; macOS has bash 3.2 and a date without %N.
now_secs() {
  perl -MTime::HiRes=time -e 'printf "%.3f\n", time'
}

# The container user's shell environment, as a login does it.
# shellcheck disable=SC2034 # read by the scripts that source this file
EXEC_ENV=(-u scientist -w /home/scientist -e HOME=/home/scientist -e USER=scientist -e LOGNAME=scientist)
