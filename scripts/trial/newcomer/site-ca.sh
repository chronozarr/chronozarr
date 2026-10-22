#!/usr/bin/env bash
# Make a throwaway CA and a certificate for chronozarr.org that it signs, for serving a local copy of the site.
#
#   scripts/trial/newcomer/site-ca.sh OUT_DIR
#
# Writes OUT_DIR/ca.pem, site.pem (certificate for chronozarr.org and www.chronozarr.org) and site.key. The CA key is
# deleted at the end, so nothing else can be signed. Valid for 7 days. Nothing is installed anywhere: the containers
# of the trial take ca.pem through SITE_CA (scripts/trial/lib.sh), and the host's trust store is never touched.
set -euo pipefail

[ "$#" -eq 1 ] || {
  echo "usage: site-ca.sh OUT_DIR" >&2
  exit 1
}
out=$1
mkdir -p "$out"
[ ! -e "$out/ca.pem" ] || {
  echo "error: $out/ca.pem exists; use another directory" >&2
  exit 1
}

openssl req -x509 -newkey rsa:2048 -nodes -days 7 -subj "/CN=chronozarr trial local CA" \
  -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -keyout "$out/ca.key" -out "$out/ca.pem" 2>/dev/null
openssl req -newkey rsa:2048 -nodes -subj "/CN=chronozarr.org" -keyout "$out/site.key" -out "$out/site.csr" 2>/dev/null
printf '%s\n' "subjectAltName=DNS:chronozarr.org,DNS:www.chronozarr.org" "basicConstraints=CA:FALSE" \
  "keyUsage=digitalSignature,keyEncipherment" "extendedKeyUsage=serverAuth" >"$out/site.ext"
openssl x509 -req -in "$out/site.csr" -CA "$out/ca.pem" -CAkey "$out/ca.key" -CAcreateserial -days 7 \
  -extfile "$out/site.ext" -out "$out/site.pem" 2>/dev/null
openssl verify -CAfile "$out/ca.pem" "$out/site.pem"
rm "$out/ca.key" "$out/site.csr" "$out/site.ext" "$out/ca.srl"
chmod 600 "$out/site.key"
openssl x509 -in "$out/site.pem" -noout -subject -ext subjectAltName -enddate
