#!/usr/bin/env bash
# One simulated newcomer session: Codex, with no project context, plays a scientist in container newcomer-N.
#
#   scripts/trial/run-codex-newcomer.sh N IMAGE DATASET_DIR ROLE DATA_NOTE WORK_ROOT
#
# ROLE ("hydrologist", ...) and DATA_NOTE (what the scientist knows about their files, in their words) fill
# newcomer/brief.md. WORK_ROOT holds the agent's working directory and its private CODEX_HOME. Codex gets a fresh
# CODEX_HOME holding only a link to the owner's auth.json and a config with memories off: no AGENTS.md, memories,
# hooks, skills or MCP servers. Env: CODEX_BIN (default codex), CODEX_MODEL (default gpt-6.1-sol),
# SESSION_TIMEOUT_S (default 3600). The environment also reaches start-newcomer.sh and recipient.sh: SITE_IP and
# SITE_CA (a local copy of chronozarr.org, see lib.sh) and RECIPIENT_IMAGE (default chronozarr-newcomer:ubuntu).
#
# Afterwards: the share link from the agent's answer goes to recipient.sh, the container's history, screenshots and
# background logs are saved, and the container is removed. Records: <main checkout>/data/trial/newcomer-N/.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib.sh
. "$here/lib.sh"

[ "$#" -eq 6 ] || die "usage: run-codex-newcomer.sh N IMAGE DATASET_DIR ROLE DATA_NOTE WORK_ROOT"
n=$1
image=$2
dataset=$3
role=$4
data_note=$5
work_root=$6
codex_bin=${CODEX_BIN:-codex}
model=${CODEX_MODEL:-gpt-6.1-sol}
timeout_s=${SESSION_TIMEOUT_S:-3600}
[ -f "$HOME/.codex/auth.json" ] || die "no ~/.codex/auth.json; sign in to Codex first"

out="$(trial_data_dir)/newcomer-$n"
ws="$work_root/nc-$n"
codex_home="$work_root/codex-home-$n"
[ ! -e "$ws" ] || die "$ws exists; use another N or move it"
mkdir -p "$ws" "$codex_home"

"$here/start-newcomer.sh" "$n" "$image" "$dataset"

ln -s "$HOME/.codex/auth.json" "$codex_home/auth.json"
printf 'model = "%s"\nmodel_reasoning_effort = "medium"\n\n[features]\nmemories = false\n' "$model" >"$codex_home/config.toml"

printf '#!/usr/bin/env bash\nexec %q %q "$@"\n' "$here/x.sh" "$n" >"$ws/x"
chmod +x "$ws/x"

ROLE=$role DATA_NOTE=$data_note perl -pe 's/\{ROLE\}/$ENV{ROLE}/g; s/\{DATA_NOTE\}/$ENV{DATA_NOTE}/g' \
  "$here/newcomer/brief.md" >"$out/brief.md"
cp "$here/newcomer/schema.json" "$out/schema.json"
{
  echo "codex:    $("$codex_bin" --version 2>&1) model $model"
  echo "role:     $role"
  echo "data:     $data_note"
  echo "workdir:  $ws"
  echo "session:  started $(now_iso)"
} | tee -a "$out/start.txt"

set +e
(cd "$ws" && CODEX_HOME=$codex_home perl -e 'alarm shift; exec @ARGV' "$timeout_s" \
  "$codex_bin" exec --ephemeral --ignore-rules --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox \
  -C "$ws" --json --output-schema "$out/schema.json" -o "$out/final.json" - \
  <"$out/brief.md" >"$out/codex-events.jsonl" 2>"$out/codex-stderr.txt")
rc=$?
set -e
echo "session:  ended $(now_iso), codex exit $rc" | tee -a "$out/start.txt"

link=""
if [ -s "$out/final.json" ]; then
  link=$(perl -MJSON::PP -0777 -ne 'my $d = decode_json($_); print $d->{share_link} // ""' "$out/final.json" || true)
fi
if [ -n "$link" ]; then
  echo "recipient: opening $link"
  "$here/recipient.sh" "$link" >"$out/recipient.txt" 2>&1 || echo "recipient.sh exit $?" >>"$out/recipient.txt"
else
  echo "recipient: no link in the answer" | tee "$out/recipient.txt"
fi

# Keep what the scientist made (stores, scripts) for checking. Their copy of the data, caches, the image's
# Miniforge and the packages inside any environment they made stay out.
docker exec -u scientist "newcomer-$n" tar -C /home/scientist --exclude=./data --exclude=./.cache \
  --exclude=./.local --exclude=./miniforge3 --exclude='*/lib/python3.*' -cf - . >"$out/home.tar" ||
  echo "could not save the home directory" >&2

"$here/stop-newcomer.sh" "$n"
