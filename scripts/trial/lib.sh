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

# A local stand-in for chronozarr.org, for measuring a release before it exists (newcomer/site_server.mjs serves it,
# newcomer/site-ca.sh makes its certificate). Both variables default to unset: the containers then reach the real site.
#   SITE_IP  where chronozarr.org (and www) resolves inside a container: an address, or host-gateway for the Docker host.
#   SITE_CA  PEM file of the CA that signed the stand-in's certificate. Each container installs it into its own trust
#            store; the host's is never touched.
# site_args fills SITE_ARGS for `docker run`; site_trust NAME makes container NAME accept the stand-in's certificate.
# shellcheck disable=SC2034 # read by the scripts that source this file
SITE_ARGS=()
site_args() {
  SITE_ARGS=()
  if [ -n "${SITE_IP:-}" ]; then
    # shellcheck disable=SC2034 # same
    SITE_ARGS=(--add-host "chronozarr.org:$SITE_IP" --add-host "www.chronozarr.org:$SITE_IP")
  fi
  if [ -n "${SITE_CA:-}" ]; then
    [ -n "${SITE_IP:-}" ] || die "SITE_CA is set without SITE_IP"
    [ -f "$SITE_CA" ] || die "SITE_CA '$SITE_CA' is not a file"
  fi
}

# One line for start.txt: how a container reaches chronozarr.org.
site_summary() {
  if [ -z "${SITE_IP:-}" ]; then
    echo "the real chronozarr.org"
  else
    echo "chronozarr.org -> $SITE_IP, certificate from the local CA $SITE_CA ($(openssl x509 -in "${SITE_CA:-/dev/null}" -noout -fingerprint -sha256 2>/dev/null || echo 'no CA'))"
  fi
}

# Container NAME trusts SITE_CA: in the system store (curl, Python, uv), and for the headless browser `look` by the
# CA's public key hash, which Chromium takes as --ignore-certificate-errors-spki-list (look reads /etc/look/trusted-spki).
# Chromium on Linux reads its own NSS database, not /etc/ssl/certs.
site_trust() {
  local name=$1 spki
  [ -n "${SITE_CA:-}" ] || return 0
  spki=$(openssl x509 -in "$SITE_CA" -pubkey -noout | openssl pkey -pubin -outform der | openssl dgst -sha256 -binary | base64)
  docker cp "$SITE_CA" "$name:/usr/local/share/ca-certificates/trial-site-ca.crt"
  # shellcheck disable=SC2016 # runs inside the container, where $1 expands
  docker exec -u root "$name" sh -c '
    chmod 644 /usr/local/share/ca-certificates/trial-site-ca.crt
    update-ca-certificates >/dev/null
    mkdir -p /etc/look
    printf "%s\n" "$1" >/etc/look/trusted-spki
    chmod -R a+rX /etc/look
    printf "%s\n" "export SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt" \
      "export REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt" >/etc/profile.d/trial-site-ca.sh
  ' sh "$spki"
}
