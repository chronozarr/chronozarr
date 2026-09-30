# Hosting a chronozarr store

A chronozarr store is a directory of static files. Any host that returns a file by path, answers byte-range requests and sends CORS headers serves it. There is nothing to run. This page turns the requirements in [spec/CHRONOZARR.md](../spec/CHRONOZARR.md) section 9 into a checklist, an upload procedure, and recipes for four hosts: S3 with CloudFront, Cloudflare R2, Google Cloud Storage, and Source Cooperative.

## 1. Checklist

`chronozarr doctor <store-url>` runs the HTTP and decode checks below against a live URL (and the decode checks against a local directory). It sends `Origin: https://tileripper.com` by default. The last column is what doctor reports when the requirement is missing (`fail` violates a MUST, `warn` a SHOULD, `info` is reported without judgement), read from `src/chronozarr/doctor.py` on 2026-09-30; if that file changes, it is the authority.

| # | Requirement | doctor check | If missing |
|---|---|---|---|
| 1 | `GET {store}/zarr.json` returns 200 and a chronozarr root group | `root zarr.json` | fail |
| 2 | `Access-Control-Allow-Origin: *` (or the viewer origin) on GET responses | `CORS on zarr.json`, `CORS on byte range ...` | fail |
| 3 | Consolidated metadata in the root `zarr.json` | `consolidated metadata` | warn |
| 4 | A bounded `Range: bytes=a-b` returns `206` with `Content-Range` | `byte range on <key>` | fail if sharded; warn if unsharded and the server answers 200 |
| 5 | `Access-Control-Expose-Headers` lists `Content-Range` (or `*`) | `CORS expose Content-Range` | fail |
| 6 | A suffix range `Range: bytes=-N` returns `206` with `Content-Range` (shard index reads) | `suffix range` | fail if sharded; warn otherwise |
| 7 | `OPTIONS` preflight answers 200 or 204 with `Access-Control-Allow-Headers` containing `Range` (or `*`) | `CORS preflight` | warn |
| 8 | `HEAD` returns 200 with `Content-Length` and CORS headers | `HEAD` | fail if sharded without `shard_bytes`; warn otherwise |
| 9 | `Cache-Control` contains `immutable`, or `max-age` of at least one day | `cache-control` | warn when the prefix looks versioned (last path segment ends in a version or date, such as `chronozarr-2`); info otherwise |
| 10 | `Timing-Allow-Origin` is sent (MAY) | `timing-allow-origin` | info; reports the value or its absence |
| 11 | The host caches objects at the edge (MAY) | `edge cache` | info; reports `cf-cache-status` and says when it is `DYNAMIC` or `BYPASS` |
| 12 | The layout validates, every level decodes, and one pixel read through plain Zarr equals the chronozarr decode | `validate`, `decode level N` | fail |

Doctor does not check these; verify them with `curl` (section 4):

| Requirement | Level in the spec | Why it matters |
|---|---|---|
| An absent key returns `404`, not a fallback page with `200` | MUST | A missing shard, `mask` or `coverage` array is meaningful: readers decode a missing shard as fill. A host that answers 200 with HTML breaks that. |
| Chunk and shard responses carry no `Content-Encoding` | MUST | The shard index stores byte offsets into the stored object. A CDN that gzips or re-encodes the body moves the bytes. |
| Shards fit the host or CDN's cacheable object size | SHOULD | Choose `shard_time` at encode time (section 5). |
| Metadata uploaded last; the prefix never rewritten | SHOULD / MUST | Section 2. |

A preflight is sent only for a suffix range. Browsers do not preflight a bounded `bytes=a-b`. A store written with `shard_bytes` lets a reader fetch every shard index as a bounded range, so checks 6 and 7 cost nothing for it in practice, but doctor still reports them. Without `Timing-Allow-Origin`, `PerformanceResourceTiming.transferSize` is 0 for cross-origin requests, so in-page benchmarks under-report bytes; the viewer then counts bytes only from `Content-Length`.

## 2. Immutable prefixes and upload order

**Immutable prefixes.** A store is never edited in place. Every encode is written under a new prefix (for example `ucayali_santa_maria/chronozarr-3`), the catalog or URL is switched to it, and the old prefix is deleted afterwards. This is what makes `Cache-Control: public, max-age=31536000, immutable` safe: browsers and CDNs may hold any object for a year, and no object at that URL ever changes.

**Metadata last.** The root `zarr.json` is the marker that a store exists. Upload in three phases:

1. Chunk and shard objects (everything except `zarr.json`), in parallel.
2. Group and array `zarr.json` below the root.
3. The root `zarr.json`.

A reader that finds the root therefore finds a complete store. Do not request the final URL before phase 3 finishes: CDNs cache a `404` for seconds to minutes, and a cached `404` on the root `zarr.json` makes a finished store look absent.

The same three phases work for every host. Save this as `upload.sh`, run it with `bash`, and define `put` from the recipe of your host. `put FILE` uploads `./FILE` (relative to the store directory) to `$PREFIX/FILE` with `Cache-Control: $CC`.

```bash
#!/usr/bin/env bash
set -euo pipefail
STORE=data/stores/ucayali_santa_maria/chronozarr-3   # local store directory
PREFIX=ucayali_santa_maria/chronozarr-3              # new for every encode
CC="public, max-age=31536000, immutable"

# put() goes here, from the host recipe below

export -f put
export STORE PREFIX CC BUCKET   # plus anything else put() reads
cd "$STORE"
# 1. chunk and shard objects
find . -type f ! -name zarr.json -print0 | xargs -0 -n 1 -P 8 bash -c 'put "$1"' _
# 2. group and array metadata below the root
find . -type f -name zarr.json ! -path ./zarr.json -print0 | xargs -0 -n 1 -P 8 bash -c 'put "$1"' _
# 3. root metadata, last
put ./zarr.json
```

`set -e` stops before phase 2 or 3 if any upload in the previous phase failed (`xargs` exits 123).

The repository's `scripts/upload_stores.sh` uploads to R2 with `Cache-Control: public, max-age=31536000, immutable` in one parallel pass, so it does not enforce metadata-last. It also lists `<aoi>/chronozarr` under `data/stores`, which does not match a versioned directory such as `chronozarr-3`, so for current stores use the procedure above. One pass is harmless while nothing links to the new prefix (for TileRipper the catalog change is the commit point: upload, run `chronozarr doctor`, then deploy the catalog); use the procedure above when a reader can reach the prefix during the upload.

## 3. Recipes

### 3.1 Amazon S3 with CloudFront

Keep the bucket private and let CloudFront read it through an origin access control (OAC). CloudFront gives HTTPS on your own domain, caching, and the only way to add `Timing-Allow-Origin` in front of S3.

```bash
BUCKET=my-chronozarr-stores
REGION=us-east-1
aws s3api create-bucket --bucket "$BUCKET" --region "$REGION"   # outside us-east-1 add --create-bucket-configuration LocationConstraint="$REGION"
aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

Create a CloudFront distribution (console or `aws cloudfront create-distribution`) with:

- Origin: the bucket's REST endpoint `BUCKET.s3.REGION.amazonaws.com`, origin access: origin access control with signing on.
- Default cache behavior: viewer protocol policy redirect HTTP to HTTPS; allowed methods `GET, HEAD, OPTIONS`; cache policy `CachingOptimized` (no headers or query strings in the cache key).
- Response headers policy: the custom policy below.

Bucket policy allowing only that distribution (fill in the account id and distribution id):

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "AllowCloudFrontRead",
    "Effect": "Allow",
    "Principal": { "Service": "cloudfront.amazonaws.com" },
    "Action": "s3:GetObject",
    "Resource": "arn:aws:s3:::my-chronozarr-stores/*",
    "Condition": { "StringEquals": { "AWS:SourceArn": "arn:aws:cloudfront::<account-id>:distribution/<distribution-id>" } }
  }]
}
```

Response headers policy (`aws cloudfront create-response-headers-policy --response-headers-policy-config file://chronozarr-headers.json`). It adds the CORS headers with `OriginOverride`, so the bucket needs no CORS configuration, and `Timing-Allow-Origin`:

```json
{
  "Name": "chronozarr-store",
  "CorsConfig": {
    "AccessControlAllowOrigins": { "Quantity": 1, "Items": ["*"] },
    "AccessControlAllowHeaders": { "Quantity": 1, "Items": ["Range"] },
    "AccessControlAllowMethods": { "Quantity": 2, "Items": ["GET", "HEAD"] },
    "AccessControlAllowCredentials": false,
    "AccessControlExposeHeaders": { "Quantity": 4, "Items": ["Content-Range", "Content-Length", "ETag", "Accept-Ranges"] },
    "AccessControlMaxAgeSec": 86400,
    "OriginOverride": true
  },
  "CustomHeadersConfig": {
    "Quantity": 1,
    "Items": [{ "Header": "Timing-Allow-Origin", "Value": "*", "Override": true }]
  }
}
```

Upload with the section 2 script, using:

```bash
put() { aws s3 cp "$1" "s3://$BUCKET/$PREFIX/${1#./}" --cache-control "$CC" --only-show-errors; }
```

The store URL is `https://<distribution-domain>/$PREFIX`. Notes:

- `Cache-Control` is stored on the objects at upload and passed through by CloudFront. Nothing needs invalidating because prefixes are immutable.
- `aws s3 cp` labels extensionless objects `binary/octet-stream`. CloudFront compresses only a fixed list of content types, between 1,000 and 10,000,000 bytes, and `binary/octet-stream` is not on it, so shards are served as stored. Confirm with section 4.
- Serving a public bucket directly (no CloudFront) also works for `Range` and `Cache-Control`. It needs an S3 CORS configuration instead of the response headers policy, and it cannot send `Timing-Allow-Origin`:

```json
{
  "CORSRules": [{
    "AllowedOrigins": ["*"],
    "AllowedMethods": ["GET", "HEAD"],
    "AllowedHeaders": ["Range"],
    "ExposeHeaders": ["Content-Range", "Content-Length", "ETag", "Accept-Ranges"],
    "MaxAgeSeconds": 86400
  }]
}
```

Apply it with `aws s3api put-bucket-cors --bucket "$BUCKET" --cors-configuration file://s3-cors.json`. S3 sends CORS headers only on requests that carry an `Origin` header.

### 3.2 Cloudflare R2

This is how `data.tileripper.com` is served (see `deploy/README.md`).

```bash
BUCKET=my-chronozarr-stores
npx wrangler r2 bucket create "$BUCKET" --location enam
npx wrangler r2 bucket cors set "$BUCKET" --file deploy/r2-cors.json --force
npx wrangler r2 bucket domain add "$BUCKET" --domain data.example.com --zone-id <zone id>
```

`deploy/r2-cors.json` uses wrangler's rule format, not the S3 array:

```json
{
  "rules": [{
    "allowed": {
      "origins": ["*"],
      "methods": ["GET", "HEAD"],
      "headers": ["Range", "If-Match", "If-None-Match", "Content-Type"]
    },
    "exposeHeaders": ["Content-Range", "Content-Length", "ETag", "Accept-Ranges"],
    "maxAgeSeconds": 86400
  }]
}
```

Upload with the section 2 script, using:

```bash
put() { npx --no-install wrangler r2 object put "$BUCKET/$PREFIX/${1#./}" --file "$1" --cache-control "$CC" --remote > /dev/null; }
```

Wrangler starts in about 2 seconds per object, so a store of around 100 objects at 8 parallel takes a minute or two; an unsharded store of thousands of objects is slow this way (use an S3 client against R2's S3 endpoint instead).

Two Cloudflare rules on the hostname make the domain cache and time correctly. Both are created in the dashboard under Rules and need zone write access; the `wrangler login` token used for deploys does not have it.

- **Cache Rule.** Cloudflare does not cache extensionless objects by default, so responses stay `cf-cache-status: DYNAMIC`. Create a Cache Rule:
  - Expression: `(http.host eq "data.example.com")`. In the builder: Field `Hostname`, Operator `equals`.
  - Cache eligibility: Eligible for cache.
  - Edge TTL: Ignore cache-control header and use this TTL, 1 year.
  - Browser TTL: Override origin and use this TTL, 1 year.

  Verified on `data.tileripper.com` on 2026-09-30: `zarr.json` returned MISS then HIT with an `age` header, and a `206` range request on a shard returned MISS then HIT. Overriding both TTLs is safe only because prefixes are immutable (section 2). Setting `Cache-Control` on each object at upload still matters for clients of the bare bucket and for any rule that respects the origin.
- **`Timing-Allow-Origin`.** Add a Transform Rule, Modify Response Header, with the same hostname expression and the action Set static: `Timing-Allow-Origin` = `*`.
- **Match on the hostname.** Put the expression in the expression editor, or use the `Hostname` field. Pasting it into a URI Full wildcard value silently matches nothing and every response stays `DYNAMIC`. The dashboard's warning that the rule "may not apply to your traffic" for the R2 hostname is a false alarm; ignore it.
- **Object size.** Cloudflare documents a 512 MB cacheable object limit on the Free, Pro and Business plans; check the current figure. A shard is about `n_time` times the compressed size of one cell-timestep (the Ucayali store has 117 timesteps and shards up to 161 MB), so a long or dense series needs a smaller `shard_time` at encode time.
- **`r2.dev`.** The public development URL (`wrangler r2 bucket dev-url enable`) is rate limited and not for production. Use it for a first check only.

### 3.3 Google Cloud Storage

```bash
BUCKET=my-chronozarr-stores
gcloud storage buckets create "gs://$BUCKET" --location=US --uniform-bucket-level-access
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --member=allUsers --role=roles/storage.objectViewer
gcloud storage buckets update "gs://$BUCKET" --cors-file=gcs-cors.json
```

`gcs-cors.json`. GCS takes one `responseHeader` list and uses it for both `Access-Control-Allow-Headers` and `Access-Control-Expose-Headers`, so `Content-Range` goes in it alongside `Range`:

```json
[{
  "origin": ["*"],
  "method": ["GET", "HEAD"],
  "responseHeader": ["Content-Type", "Range", "Content-Range", "Content-Length", "ETag", "Accept-Ranges"],
  "maxAgeSeconds": 86400
}]
```

Upload with the section 2 script, using:

```bash
put() { gcloud storage cp "$1" "gs://$BUCKET/$PREFIX/${1#./}" --cache-control="$CC" --quiet; }
```

The store URL is `https://storage.googleapis.com/$BUCKET/$PREFIX`. Notes:

- CORS is honored on `storage.googleapis.com`, not on the cookie-authenticated `storage.cloud.google.com`.
- A public object with no `Cache-Control` is served with `public, max-age=3600`; the upload command above overrides it.
- `allUsers` is rejected when the organization enforces public access prevention. Either change that policy for this bucket or front the bucket with a backend bucket behind Cloud CDN.
- `Timing-Allow-Origin` cannot be set on a bucket or object. It is available as a custom response header on a backend bucket behind an external HTTPS load balancer (`gcloud compute backend-buckets create ... --custom-response-header='Timing-Allow-Origin: *'`). It is a MAY; skip it unless you benchmark the viewer.
- Do not upload objects with `Content-Encoding: gzip`; GCS applies transcoding that changes the bytes it serves.

### 3.4 Source Cooperative

[Source Cooperative](https://source.coop) (Radiant Earth) hosts open datasets and serves them through a data proxy at `https://data.source.coop`. Objects are addressed as `https://data.source.coop/<account>/<product>/<path>`. The proxy is documented as beta, and the storage behind it (S3, GCS, Azure, R2) is not your choice, so you do not configure CORS or cache headers.

Observed on one public object on 2026-09-30 (`kerner-lab/fields-of-the-world`): a bounded range returns `206` with `Content-Range` and `Accept-Ranges: bytes`; `Access-Control-Allow-Origin: *`, `Access-Control-Allow-Headers: *`, `Access-Control-Expose-Headers: *`; `OPTIONS` returns `204`; there is no `Cache-Control` and no `Timing-Allow-Origin`; `cf-cache-status: DYNAMIC`. Expect `chronozarr doctor` to pass checks 1 to 8 and 12, warn on `cache-control` for a versioned prefix, and report `edge cache` and `timing-allow-origin` as info.

Upload needs an account and upload access for the product (contact hello@source.coop if the product page shows no upload option). Two routes:

- **UI.** Product page, lock icon, Edit Mode, drag in files or directories. Uploads in the UI are not ordered: add the level directories (`0/`, `1/`, ...) and `volatility/` first, then the root `zarr.json` by itself. A sharded store has roughly a hundred objects, which is workable here; an unsharded store of thousands of objects is not.
- **S3 client through the proxy**, with temporary credentials from the Source CLI (`source-coop login`, then an AWS profile named `source-coop` as described in the Source docs). Use the section 2 script with:

```bash
ACCOUNT=my-account
PRODUCT=my-product
put() {
  aws s3 cp "$1" "s3://$ACCOUNT/$PRODUCT/$PREFIX/${1#./}" \
    --endpoint-url https://data.source.coop --profile source-coop \
    --cache-control "$CC" --only-show-errors
}
```

Whether the proxy stores and serves the `--cache-control` value is untested here; check the `cache-control` line of doctor. The store URL is `https://data.source.coop/$ACCOUNT/$PRODUCT/$PREFIX`.

## 4. Verifying by hand

```bash
URL=https://data.example.com/aoi/chronozarr-2
KEY=0/data/c/0/0/0/0          # level 0, time shard 0, cell (0, 0) of a sharded store
O='Origin: https://tileripper.com'

# 206 + Content-Range + CORS + exposed headers + caching
curl -s -D - -o /dev/null -H "$O" -H 'Range: bytes=0-99' "$URL/$KEY"
# suffix range (shard index)
curl -s -D - -o /dev/null -H "$O" -H 'Range: bytes=-16' "$URL/$KEY"
# preflight
curl -s -D - -o /dev/null -X OPTIONS -H "$O" -H 'Access-Control-Request-Method: GET' \
  -H 'Access-Control-Request-Headers: range' "$URL/$KEY"
# no Content-Encoding on a shard, even when the client offers compression
curl -s -D - -o /dev/null -H "$O" -H 'Accept-Encoding: gzip, br' -H 'Range: bytes=0-99' "$URL/$KEY" | grep -i '^content-encoding' || echo "no content-encoding: ok"
# absent keys are 404
curl -s -o /dev/null -w '%{http_code}\n' "$URL/no-such-key"
```

For an unsharded store the first data chunk is `0/data/c/0/0/0/0` as well (timestep 0, band 0, cell (0, 0)). The expected answers are `206` with `Content-Range: bytes 0-99/<length>`, `Access-Control-Allow-Origin: *`, an `Access-Control-Expose-Headers` that lists `Content-Range`, `204` or `200` for the preflight with `Range` allowed, and `404` for the missing key. Then run `chronozarr doctor "$URL"`.

## 5. Pitfalls

- **Compression in front of the store.** A CDN, proxy or bucket setting that adds `Content-Encoding` to shard objects breaks reads: the index offsets are in stored bytes and a `Range` header applies to the encoded representation. Turn compression off for the store path, or rely on content types the host does not compress.
- **Cloudflare rules that match nothing.** A Cache Rule or Transform Rule whose expression was pasted into a URI wildcard value instead of matching `Hostname` applies to no request, and R2 responses stay `DYNAMIC` with no `Timing-Allow-Origin` (section 3.2).
- **Fallback pages.** Static hosts configured for single-page apps answer unknown paths with `200` and `index.html`. The store then appears to have every shard. Serve the store from a host or prefix with real `404` behaviour.
- **Rewriting a prefix.** With `immutable` and a one-year `max-age`, a rewritten object stays stale in browsers and CDNs for up to a year. Always re-encode to a new prefix.
- **Local testing.** `python -m http.server` ignores `Range`, so sharded stores read whole shards (doctor reports it as a failure). Use `chronozarr.view(store)` from a notebook or any range-capable static server.
- **Shard size.** `shard_time` defaults to `n_time`, so one shard holds a cell's whole time axis. Lower it (and keep it a multiple of `anchor_interval` for star-delta stores) when a shard would exceed what the host or CDN caches or serves in one object.

## 6. The live store

The published demo store is `ucayali_santa_maria/chronozarr-3` (plain encoding; see the README status). It replaces `chronozarr-2`, which was live on 2026-09-30 when `chronozarr doctor` was run against `https://data.tileripper.com/ucayali_santa_maria/chronozarr-2`: 15 ok, 2 info, 0 warnings (`edge cache` HIT, `timing-allow-origin` `*`, `cache-control` `max-age=31536000`). The host is R2 with `deploy/r2-cors.json` plus the Cache Rule and the Transform Rule of section 3.2; both rules match the hostname, so they apply to every prefix under it.
