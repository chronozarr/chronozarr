# Hosting a chronozarr store

A chronozarr store is a directory of static files. A host serves it with no server code. The host must return a file by path and send CORS headers. A sharded store also needs byte-range requests.

The prefix of a store is its path in the bucket, for example `my_aoi/chronozarr-2`.

The recipes below publish a store that anyone with its URL can read. For data that must stay private, see [private.md](private.md).

This page has a checklist, an upload procedure with a command that runs it, recipes for four hosts and a procedure for appending. The requirements come from [spec/CHRONOZARR.md](../spec/CHRONOZARR.md) section 8. Measurements and dates are in [evidence.md](evidence.md).

## 1. Checklist

`chronozarr doctor <store-url>` runs the HTTP checks and the decode checks against a live URL. Against a local directory, it runs only the decode checks. It sends `Origin: https://chronozarr.org`. The `--origin` option changes that header.

The source of the checks is `src/chronozarr/doctor.py`.

Doctor reports one of three levels when a requirement is missing:

- `fail` means a browser reader cannot read the store, or the layout or a decode is wrong.
- `warn` means advice: the store breaks a SHOULD in the spec, or readers pay a measurable cost.
- `info` reports a fact with no judgement.

Doctor exits with status 1 only when a check fails. Warnings and info lines do not change the exit status.

| # | Requirement | doctor check | Sharded store | Unsharded store |
|---|---|---|---|---|
| 1 | `GET {store}/zarr.json` returns 200 and a chronozarr root group | `root zarr.json` | fail | fail |
| 2 | `Access-Control-Allow-Origin: *` (or the viewer origin) is on GET responses | `CORS on zarr.json`, `CORS on byte range ...` | fail | fail |
| 3 | The root `zarr.json` holds consolidated metadata | `consolidated metadata` | warn | warn |
| 4 | A bounded `Range: bytes=a-b` returns `206` with `Content-Range` | `byte range on <key>` | fail | warn |
| 5 | `Access-Control-Expose-Headers` lists `Content-Range` (or `*`) | `CORS expose Content-Range` | fail | fail |
| 6 | A suffix range `Range: bytes=-N` returns `206` with `Content-Range` | `suffix range` | fail | info |
| 7 | An `OPTIONS` preflight returns 200 or 204 with `Access-Control-Allow-Headers` that contains `Range` (or `*`) | `CORS preflight` | warn | info |
| 8 | `HEAD` returns 200 with `Content-Length` and CORS headers | `HEAD` | fail | warn |
| 9 | `Cache-Control` contains `immutable`, or `max-age` is at least one day | `cache-control` | warn | warn |
| 10 | `Timing-Allow-Origin` is sent (MAY) | `timing-allow-origin` | info | info |
| 11 | The host caches objects at the edge (MAY) | `edge cache` | info | info |
| 12 | The layout validates, every level decodes, and a pixel read through plain Zarr equals the chronozarr decode | `validate`, `decode level N` | fail | fail |

Notes on the table:

- Row 4, unsharded: doctor warns when the server answers 200 to a range request.
- Row 6: a suffix range reads the shard index. An unsharded store has no shard index.
- Row 8, sharded: doctor reports `warn` instead of `fail` when the store has `shard_bytes`.
- Row 9: doctor reports `warn` when the last path segment of the prefix ends in a version or a date, such as `chronozarr-2`. Otherwise it reports `info`.
- Row 10: doctor reports the value of the header, or that the header is absent.
- Row 11: doctor reports `cf-cache-status`. It says when the status is `DYNAMIC` or `BYPASS`.

Doctor does not check the requirements below. Verify them with `curl` (section 4).

| Requirement | Spec level | Why it matters |
|---|---|---|
| An absent key returns `404` | MUST | A missing shard, `mask` or `coverage` array is meaningful. Readers decode a missing shard as fill. A host that answers 200 with HTML breaks that. |
| Chunk and shard responses carry no `Content-Encoding` | MUST | The shard index stores byte offsets into the stored object. A CDN that gzips or re-encodes the body moves the bytes. |
| Shards fit the cacheable object size of the host or CDN (sharded stores) | SHOULD | Choose `shard_time` at encode time (section 5). An unsharded store has one chunk per object. The largest object of the live store is 1.8 MB. |
| Metadata is uploaded last | SHOULD | See section 2. |
| The prefix is never rewritten | MUST | See section 2. |

Preflight and range notes:

- A browser sends a preflight only for a suffix range. It sends no preflight for a bounded `bytes=a-b`.
- A store written with `shard_bytes` lets a reader fetch every shard index as a bounded range. Rows 6 and 7 then cost nothing in practice. Doctor still reports them.
- An unsharded store is the encoder default. A reader fetches it with plain `GET` requests and no `Range` header.
- Without `Timing-Allow-Origin`, a benchmark that reads resource timing under-reports bytes. Details: [evidence.md](evidence.md#timing-allow-origin).

## 2. Immutable prefixes and upload order

### Immutable prefixes

Never edit a store in place. Write every encode under a new prefix. Then switch the catalog or URL to the new prefix. Delete the old prefix afterwards.

This rule makes `Cache-Control: public, max-age=31536000, immutable` safe. Browsers and CDNs hold any object for a year. No object at that URL ever changes.

### Metadata last

The root `zarr.json` marks that a store exists. Upload in three phases:

1. Upload the chunk and shard objects (everything except `zarr.json`), in parallel.
2. Upload the group and array `zarr.json` files below the root.
3. Upload the root `zarr.json`.

A reader that finds the root then finds a complete store.

`chronozarr publish` splits phase 1 in two. It uploads the `time/c/0` and `volatility` chunks after the other chunks, because an append rewrites only those.

Do not request the final URL before phase 3 ends. A CDN caches a `404` for seconds to minutes. A cached `404` on the root `zarr.json` makes a finished store look absent.

### `chronozarr publish`

`chronozarr publish STORE` is available in the released Python package 0.4.0. It runs the four phases below against an S3 bucket, an S3-compatible bucket such as Cloudflare R2, a Google Cloud Storage bucket or an Azure Blob Storage container. It then checks the hosted store with `chronozarr doctor` and prints a viewer link. The destination scheme picks the provider:

| Provider | Destination | Extra | SDK | Credentials |
|---|---|---|---|---|
| AWS S3, Cloudflare R2 | `s3://BUCKET/PREFIX` | `publish` | boto3 | boto3's chain |
| Google Cloud Storage | `gs://BUCKET/PREFIX` | `publish-gcs` | google-cloud-storage | Application Default Credentials |
| Azure Blob Storage | `az://ACCOUNT/CONTAINER/PREFIX` | `publish-azure` | azure-storage-blob, azure-identity | `DefaultAzureCredential`, or `AZURE_STORAGE_CONNECTION_STRING` |

Each provider has its own extra, so you install one SDK only. For example: `uv sync --extra publish-azure` or `pip install 'chronozarr[publish-azure]==0.4.0'`. Package 0.4.0 still writes the v0.3 store format.

```bash
# AWS S3. The credentials come from boto3's chain: environment, ~/.aws, SSO or an instance role.
chronozarr publish my_store \
  --destination s3://my-bucket/aoi/store-v1 \
  --public-url https://d111111abcdef8.cloudfront.net/aoi/store-v1 \
  --profile research

# Cloudflare R2. The endpoint is authenticated, so --public-url is required.
chronozarr publish my_store \
  --destination s3://my-bucket/aoi/store-v1 \
  --endpoint-url https://<account id>.r2.cloudflarestorage.com \
  --public-url https://data.example.com/aoi/store-v1

# Google Cloud Storage. Run `gcloud auth application-default login` once, or set
# GOOGLE_APPLICATION_CREDENTIALS. Without --public-url the store URL is
# https://storage.googleapis.com/my-bucket/aoi/store-v1.
chronozarr publish my_store \
  --destination gs://my-bucket/aoi/store-v1

# Azure Blob Storage. Run `az login` once, or set the AZURE_CLIENT_ID, AZURE_TENANT_ID and
# AZURE_CLIENT_SECRET of a service principal. The destination names the storage account, the
# container and the prefix. Without --public-url the store URL is
# https://myaccount.blob.core.windows.net/stores/aoi/store-v1.
chronozarr publish my_store \
  --destination az://myaccount/stores/aoi/store-v1
```

`--profile`, `--endpoint-url` and `--region` are S3 options. The command rejects them with a `gs://` or `az://` destination.

Add `--dry-run` to print the plan and what the prefix already holds, with nothing uploaded.

The destination and the public URL are two addresses:

| | Destination | Public URL |
|---|---|---|
| Form | `s3://BUCKET/PREFIX`, `gs://BUCKET/PREFIX` or `az://ACCOUNT/CONTAINER/PREFIX` | `https://HOST/PREFIX` |
| Used by | the upload, with your credentials | browsers and the viewer link |
| Source | `--destination` | `--public-url`, or without a CDN the HTTPS endpoint of the bucket (AWS S3, Google Cloud Storage, Azure Blob Storage) |

An `s3://` address is rejected as a public URL, and so is a URL with credentials, a query string (a signed URL) or a fragment, because the URL ends up in a link that other people open. For R2 there is no default: the storage endpoint is authenticated, so give the custom domain or the public endpoint of the bucket. For AWS the default `https://BUCKET.s3.REGION.amazonaws.com/PREFIX` serves objects only when the bucket allows public reads. For a private bucket behind CloudFront (section 3.1), pass the distribution URL. For Google Cloud Storage the default is `https://storage.googleapis.com/BUCKET/PREFIX`. It serves objects only when the bucket allows anonymous reads. The command never uses `storage.cloud.google.com`, because that host authenticates with cookies and does not answer CORS. For Azure the default is `https://ACCOUNT.blob.core.windows.net/CONTAINER/PREFIX`. It serves objects only when the storage account allows anonymous access and the container's public access level is Blob.

The Azure destination names the storage account because a container name alone does not say which account to write to. An `https://ACCOUNT.blob.core.windows.net/...` address is not accepted as a destination. Pass it as `--public-url`.


What the command does, in order:

1. Validates the store, as `chronozarr validate` does. A store that fails is not uploaded.
2. Prints the plan: destination, public URL, object count, bytes, the four phases and the cache headers.
3. Lists the prefix. A prefix that holds only objects identical to the store (same size and MD5) is an interrupted upload and resumes. A prefix that holds any other object is refused. Use a new prefix for every version. A store that you published and then appended to is the exception: use `--update` ([section 7](#publishing-an-append)). `--overwrite` replaces objects whose content differs. It never deletes, and it leaves a one-year cached copy stale for readers that already loaded it.
4. Uploads in four phases: data chunks, then the time and volatility chunks, then the group and array `zarr.json` files, then the root `zarr.json`. A phase starts after the previous one is fully stored. If an upload fails, the command stops with no root `zarr.json` in the bucket and names the failing key. Running the same command again skips the stored objects, so a retry repeats no work and a finished upload is a no-op. boto3 retries each request up to five times before a failure is reported. The Google client retries transient errors with its default policy, which the command requests explicitly for every upload. The Azure client retries each request up to five times.
5. Runs the doctor checks against the public URL, with `Origin: https://chronozarr.org`.
6. Prints `https://chronozarr.org/demo/?store=<URL-encoded public URL>` only when no check failed. Warnings are printed and do not block the link. When a check fails, the objects stay in the bucket, no link is printed and the exit status is 1.

Cache headers follow the table in section 7. Every `zarr.json`, each level's `time/c/0` and the `volatility` chunks get `public, max-age=300`. Every other object gets `public, max-age=31536000, immutable`. The command does not mark the last shard of a sharded store as short-lived. `--update` therefore refuses a sharded append that rewrites a trailing shard (section 7).

CORS and public access:

- The command reads the bucket's CORS rules only when the doctor reports a CORS failure. Without `--apply-cors` it prints the rule the viewer needs and the number of rules that exist, and changes nothing.
- With `--apply-cors` it writes the existing rules unchanged, in their order, followed by the viewer rule (the rule of `deploy/r2-cors.json`), and runs the checks again. Putting the new rule last means no request that an existing rule answers today changes its answer. If an earlier rule already matches the viewer's requests without the headers it needs, the checks still fail and you merge the rules by hand.
- Google Cloud Storage keeps CORS on the bucket as a list of rules. The command reads it with `storage.buckets.get` and writes it with `storage.buckets.update`. The viewer rule is the one in section 3.3.
- Azure keeps CORS on the Blob service of the storage account. One rule list applies to every container in the account. The command keeps the existing rules, appends the viewer rule and writes the whole list. Azure allows five rules, and the command refuses to write a sixth. Reading and writing the list needs the Storage Account Contributor role (Microsoft.Storage/storageAccounts/blobServices/read and write), or an account key in `AZURE_STORAGE_CONNECTION_STRING`.
- S3 CORS needs `s3:GetBucketCORS` and `s3:PutBucketCORS`. An R2 token with Object Read and Write cannot manage CORS. Use a token with bucket admin permission, or run `npx wrangler r2 bucket cors set` (section 3.2).
- When a bucket sits behind CloudFront, CORS comes from the response headers policy (section 3.1), and the bucket's CORS rules are not what the checks see.
- The command never changes a bucket's public access block, bucket policy, IAM bindings, custom domains or cache rules. For Google Cloud Storage that includes `allUsers` grants and public access prevention. For Azure it includes the account's anonymous access setting and the container's public access level. When the objects are stored but the doctor gets 403 or 404, it prints what to change in the provider's console. Setting CORS does not make private objects readable.

The command does not create buckets. The dataset stays in your bucket, and its storage and delivery charges are yours. chronozarr.org serves the viewer and holds no data.

### The upload script

The three phases work for every host. The script below runs them.

1. Save the script as `upload.sh`.
2. Define `put` from the recipe for your host.
3. Run `bash upload.sh`.

`put FILE` uploads `./FILE`, relative to the store directory, to `$PREFIX/FILE` with `Cache-Control: $CC`.

```bash
#!/usr/bin/env bash
set -euo pipefail
STORE=data/stores/my_aoi/chronozarr-2   # local store directory
PREFIX=my_aoi/chronozarr-2              # new for every encode
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

If an upload fails in phase 1 or 2, `xargs` exits with 123. Then `set -e` stops the script before the next phase.

### The R2 upload script

`scripts/upload_stores.sh` uploads to R2 in the same three phases. It stops before the next phase when an upload fails.

It reads the store from `data/stores/<aoi>/$STORE`. Set `STORE=chronozarr-4` for a versioned prefix.

It sets `Cache-Control` for each object:

- Every chunk and shard gets `immutable`, except the objects that an append rewrites.
- The objects that an append rewrites get `max-age=300`. Section 7 lists them.
- `--trailing-ttl SECONDS` changes the 300.
- `--dry-run` prints the class of every object and uploads nothing.

## 3. Recipes

### 3.1 Amazon S3 with CloudFront

Keep the bucket private. CloudFront reads it through an origin access control (OAC). The distribution in this recipe is public: anyone with its URL reads the store ([private.md](private.md)). CloudFront gives HTTPS on your own domain and caching. It is also the only way to add `Timing-Allow-Origin` in front of S3.

1. Create the bucket and block public access.

   ```bash
   BUCKET=my-chronozarr-stores
   REGION=us-east-1
   aws s3api create-bucket --bucket "$BUCKET" --region "$REGION"   # outside us-east-1 add --create-bucket-configuration LocationConstraint="$REGION"
   aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
     BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
   ```

2. Create a CloudFront distribution in the console or with `aws cloudfront create-distribution`. Use these settings:

   - Origin: the REST endpoint of the bucket, `BUCKET.s3.REGION.amazonaws.com`.
   - Origin access: origin access control, with signing on.
   - Viewer protocol policy: redirect HTTP to HTTPS.
   - Allowed methods: `GET, HEAD, OPTIONS`.
   - Cache policy: `CachingOptimized`. The cache key has no headers and no query strings.
   - Response headers policy: the custom policy from step 4.

3. Attach this bucket policy. It allows only that distribution. Fill in the account id and the distribution id.

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

4. Create the response headers policy with `aws cloudfront create-response-headers-policy --response-headers-policy-config file://chronozarr-headers.json`. The policy adds the CORS headers with `OriginOverride`, so the bucket needs no CORS configuration. It also adds `Timing-Allow-Origin`.

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

5. Upload with the script from section 2. Define `put` as:

   ```bash
   put() { aws s3 cp "$1" "s3://$BUCKET/$PREFIX/${1#./}" --cache-control "$CC" --only-show-errors; }
   ```

The store URL is `https://<distribution-domain>/$PREFIX`.

Notes:

- The upload stores `Cache-Control` on each object. CloudFront passes it through. You do not invalidate anything, because prefixes are immutable.
- `aws s3 cp` labels extensionless objects `binary/octet-stream`. CloudFront compresses only a fixed list of content types, for objects between 1,000 and 10,000,000 bytes. `binary/octet-stream` is not on the list. Shards are served as stored. Confirm this with section 4.
- A public bucket without CloudFront also serves `Range` and `Cache-Control`. It needs an S3 CORS configuration instead of the response headers policy. It cannot send `Timing-Allow-Origin`.
- S3 sends CORS headers only on requests that carry an `Origin` header.

S3 CORS configuration for a public bucket:

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

Apply it with `aws s3api put-bucket-cors --bucket "$BUCKET" --cors-configuration file://s3-cors.json`.

### 3.2 Cloudflare R2

`data.chronozarr.org` is served this way. See `deploy/README.md`.

1. Create the bucket, set its CORS rules and add a custom domain.

   ```bash
   BUCKET=my-chronozarr-stores
   npx wrangler r2 bucket create "$BUCKET" --location enam
   npx wrangler r2 bucket cors set "$BUCKET" --file deploy/r2-cors.json --force
   npx wrangler r2 bucket domain add "$BUCKET" --domain data.example.com --zone-id <zone id>
   ```

   `deploy/r2-cors.json` uses wrangler's rule format. The S3 CORS array format does not work here:

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

2. Upload with the script from section 2. Define `put` as:

   ```bash
   put() { npx --no-install wrangler r2 object put "$BUCKET/$PREFIX/${1#./}" --file "$1" --cache-control "$CC" --remote > /dev/null; }
   ```

3. Create a Cache Rule and a Transform Rule on the hostname. Both are in the dashboard under Rules. Both need zone write access. The `wrangler login` token used for deploys does not have it.

Wrangler needs about 2 seconds per object. An unsharded store has thousands of objects. For the first upload of an unsharded store, use an S3 client against the S3 endpoint of R2. Each later append uploads only the objects it wrote (section 7). Upload times are in [evidence.md](evidence.md#live-store-and-publishing).

#### Cache Rule

Cloudflare does not cache objects without a file extension by default. Chunk keys have none, so their responses stay `cf-cache-status: DYNAMIC` until you add a Cache Rule.

Create the rule in the dashboard under Caching, then Cache Rules:

1. Set the field to `Hostname`, the operator to `equals`, and the value to your data hostname. The expression is `(http.host eq "data.example.com")`.
2. Set Cache eligibility to Eligible for cache.
3. Set Edge TTL to Use cache-control header if present, bypass cache if not.
4. Under Caching, then Configuration, set Browser Cache TTL to Respect Existing Headers.

The upload script sets `Cache-Control` on every object (section 7 lists the classes). The rule follows those headers. Chunks stay cached for a year, and `zarr.json` expires after 300 seconds, so an append shows up within five minutes.

Step 3 also keeps `404` responses out of the cache. A missing chunk means fill, and an append later creates it.

Step 4 matters. The zone default raises any shorter browser lifetime to four hours, so `zarr.json` would reach browsers with `max-age=14400`.

Do not match on a URI Full wildcard. That expression matches nothing, and every response stays `DYNAMIC`. The dashboard warns that a hostname rule "may not apply to your traffic" for an R2 hostname. The warning is wrong for this case. The verification is in [evidence.md](evidence.md#cloudflare-cache-rule).

#### `Timing-Allow-Origin`

Add a Transform Rule of type Modify Response Header. Use the same hostname expression. Set the action to Set static, with `Timing-Allow-Origin` = `*`.

#### Object size and miss cost

A cache miss on a range read pulls the whole object from R2, so the cost grows with object size. In the live unsharded store the largest chunk object is 1.8 MB, and a miss is cheap. In a sharded store, a smaller `shard_time` makes a miss cheaper. Cloudflare limits the size of a cacheable object by plan. Measurements are in [evidence.md](evidence.md#layout-choice).

#### One cached copy per requesting site

With a CORS policy, R2 sends `Vary: Origin`, so Cloudflare caches a separate copy of each object for each site that requests it. Visitors of one site share a cache. A site that serves the reader or the viewer from its own origin starts with a cache miss on every object. So does a client that sends no `Origin` header (Python, GDAL, curl). An iframe of the hosted viewer sends `Origin: https://chronozarr.org`. A Transform Rule that removes `Vary` does not merge the copies. Measurements are in [evidence.md](evidence.md#cache-copies-per-requesting-site).

#### `r2.dev`

The public development URL (`wrangler r2 bucket dev-url enable`) is rate limited. Use it for a first check, and use a custom domain for anything public.

### 3.3 Google Cloud Storage

1. Create the bucket, make it public and set its CORS rules.

   ```bash
   BUCKET=my-chronozarr-stores
   gcloud storage buckets create "gs://$BUCKET" --location=US --uniform-bucket-level-access
   gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --member=allUsers --role=roles/storage.objectViewer
   gcloud storage buckets update "gs://$BUCKET" --cors-file=gcs-cors.json
   ```

   GCS takes one `responseHeader` list. It uses the list for both `Access-Control-Allow-Headers` and `Access-Control-Expose-Headers`. Put `Content-Range` in the list next to `Range`. This is `gcs-cors.json`:

   ```json
   [{
     "origin": ["*"],
     "method": ["GET", "HEAD"],
     "responseHeader": ["Content-Type", "Range", "Content-Range", "Content-Length", "ETag", "Accept-Ranges"],
     "maxAgeSeconds": 86400
   }]
   ```

2. Upload with the script from section 2. Define `put` as:

   ```bash
   put() { gcloud storage cp "$1" "gs://$BUCKET/$PREFIX/${1#./}" --cache-control="$CC" --quiet; }
   ```

The store URL is `https://storage.googleapis.com/$BUCKET/$PREFIX`.

Notes:

- GCS honors CORS on `storage.googleapis.com`. It does not honor CORS on `storage.cloud.google.com`, which authenticates with cookies.
- A public object with no `Cache-Control` is served with `public, max-age=3600`. The upload command above overrides it.
- GCS rejects `allUsers` when the organization enforces public access prevention. Change that policy for this bucket, or put a backend bucket behind Cloud CDN.
- You cannot set `Timing-Allow-Origin` on a bucket or an object. A backend bucket behind an external HTTPS load balancer can send it as a custom response header: `gcloud compute backend-buckets create ... --custom-response-header='Timing-Allow-Origin: *'`. The header is a MAY. Skip it unless you benchmark the viewer.
- Do not upload objects with `Content-Encoding: gzip`. GCS applies transcoding, which changes the bytes it serves.

### 3.4 Source Cooperative

[Source Cooperative](https://source.coop) (Radiant Earth) hosts open datasets. It serves them through a data proxy at `https://data.source.coop`. An object has the address `https://data.source.coop/<account>/<product>/<path>`.

You do not configure CORS or cache headers on this host. The proxy is in beta. The storage behind it (S3, GCS, Azure or R2) is not your choice.

Expect `chronozarr doctor` to pass checks 1 to 8 and 12. It warns on `cache-control` for a versioned prefix. It reports `edge cache` and `timing-allow-origin` as info. Observations: [evidence.md](evidence.md#source-cooperative).

You need an account and upload access for the product. If the product page shows no upload option, write to hello@source.coop.

Choose one of two upload routes.

#### Route 1: the web UI

1. Open the product page.
2. Click the lock icon, then Edit Mode.
3. Drag in the level directories (`0/`, `1/`, ...), and `volatility/` if the store has one.
4. Drag in the root `zarr.json` by itself, last.

The UI does not order uploads, so step 4 is a separate action. A sharded store (`--shard`) has about a hundred objects, which is workable here. An unsharded store has thousands of objects, which is not.

#### Route 2: an S3 client through the proxy

1. Run `source-coop login` to get temporary credentials.
2. Create an AWS profile named `source-coop`, as the Source docs describe.
3. Upload with the script from section 2. Define `put` as:

   ```bash
   ACCOUNT=my-account
   PRODUCT=my-product
   put() {
     aws s3 cp "$1" "s3://$ACCOUNT/$PRODUCT/$PREFIX/${1#./}" \
       --endpoint-url https://data.source.coop --profile source-coop \
       --cache-control "$CC" --only-show-errors
   }
   ```

The store URL is `https://data.source.coop/$ACCOUNT/$PRODUCT/$PREFIX`.

Check the `cache-control` line of doctor. The proxy may not store the `--cache-control` value.

## 4. Verifying by hand

Run these commands against your store.

```bash
URL=https://data.example.com/aoi/chronozarr-2
KEY=0/data/c/0/0/0/0          # level 0, cell (0, 0): timestep 0 (unsharded) or time shard 0 (sharded)
O='Origin: https://chronozarr.org'

# 206 + Content-Range + CORS + exposed headers + caching
curl -s -D - -o /dev/null -H "$O" -H 'Range: bytes=0-99' "$URL/$KEY"
# suffix range (shard index; only sharded stores need it)
curl -s -D - -o /dev/null -H "$O" -H 'Range: bytes=-16' "$URL/$KEY"
# preflight
curl -s -D - -o /dev/null -X OPTIONS -H "$O" -H 'Access-Control-Request-Method: GET' \
  -H 'Access-Control-Request-Headers: range' "$URL/$KEY"
# no Content-Encoding on a chunk or shard, even when the client offers compression
curl -s -D - -o /dev/null -H "$O" -H 'Accept-Encoding: gzip, br' -H 'Range: bytes=0-99' "$URL/$KEY" | grep -i '^content-encoding' || echo "no content-encoding: ok"
# absent keys are 404
curl -s -o /dev/null -w '%{http_code}\n' "$URL/no-such-key"
```

The key `KEY` names the chunk of timestep 0, band 0, cell (0, 0) in an unsharded store. In a sharded store it names the first shard. The writer does not store an all-fill chunk. If the store is empty at that cell, the host answers `404`. Pick another cell.

Expect these answers:

- `206` with `Content-Range: bytes 0-99/<length>`.
- `Access-Control-Allow-Origin: *`.
- An `Access-Control-Expose-Headers` header that lists `Content-Range`.
- `204` or `200` for the preflight, with `Range` allowed.
- `404` for the missing key.

Then run `chronozarr doctor "$URL"`.

## 5. Pitfalls

### Compression in front of the store

A CDN, proxy or bucket setting can add `Content-Encoding` to chunk or shard objects. That breaks reads. A reader decodes each chunk with the codecs named in the store metadata and expects the stored bytes unchanged. A shard index holds offsets into the stored bytes, and a `Range` header counts bytes of the encoded response. Turn compression off for the store path, or use content types that the host does not compress.

### Fallback pages

A static host set up for single-page apps answers unknown paths with `200` and `index.html`. The store then seems to have every shard. Serve the store from a host or prefix that returns a real `404`.

### Rewriting a prefix

With `immutable` and a one-year `max-age`, a rewritten object stays stale in browsers and CDNs for up to a year. Always re-encode to a new prefix. The only in-place change is an append, and it changes only the objects in section 7.

### Local testing

`python -m http.server` ignores `Range`. A sharded store then reads whole shards, and doctor reports a failure. An unsharded store reads fine. For a sharded store, use `chronozarr preview STORE`, `chronozarr.view(store)` from a notebook, or any static server that supports ranges. To show a local store to someone on another machine without a bucket, see [Share a local store instantly](share.md).

### Shard size

A store is unsharded unless you pass `--shard`. With `--shard`, `shard_time` defaults to `n_time`, so one shard holds the whole time axis of a cell. Lower `shard_time` in two cases. First, when a shard would exceed the object size that the host or CDN caches or serves. Second, when a CDN miss on a shard is too slow (section 3.2). Keep a store that will grow unsharded (section 7).

## 6. The live store

The demo catalog lists `ucayali_santa_maria_v03` and `ucayali_santa_maria/png-v03`. R2 serves them with `deploy/r2-cors.json` and the Cache Rule from section 3.2. The history of its prefixes and its doctor results are in [evidence.md](evidence.md#live-store-and-publishing).

## 7. Appending to a live store

`chronozarr append STORE INPUT` adds timesteps at the end of a store, in place (spec section 8.3). It writes the shards or chunks that gain data and the metadata. Every other object keeps its bytes and its cache entry.

### Choose a layout

Use the default unsharded layout for stores that grow.

- An unsharded append writes only new chunk objects and the metadata. The store has no shard index that an open viewer can hold stale.
- A sharded append rewrites the whole shard that receives the new timestep.
- The price of unsharded is object count: about 5,900 objects for 117 months, against 93 sharded.
- Choose a finite `--shard-time` (12 for monthly data) only when object count matters more than the rewrite cost.
- Choose the whole-axis `--shard-time` for archives that you do not append to.

Costs and measurements: [evidence.md](evidence.md#appending) and [append.md](append.md).

### Procedure

1. Convert the new timestep or timesteps into a store.

   ```bash
   chronozarr convert new_month.csv work/new_month      # one new timestep, or encode/convert several
   ```

2. Copy the live store. An append is not atomic, so you work on a copy.

   ```bash
   mkdir -p work/aoi-working
   cp -R data/stores/aoi/chronozarr-4 work/aoi-working/chronozarr-4
   touch work/stamp                                       # only for scripts/upload_stores.sh
   ```

3. Append to the copy.

   ```bash
   chronozarr append work/aoi-working/chronozarr-4 work/new_month
   ```

4. Validate the copy.

   ```bash
   chronozarr validate work/aoi-working/chronozarr-4
   ```

5. Publish the append. `chronozarr publish --update` compares the copy with the prefix and uploads only the new and changed objects. It keeps the public URL, so the shared viewer link stays valid.

   ```bash
   chronozarr publish work/aoi-working/chronozarr-4 \
     --destination s3://my-bucket/aoi/chronozarr-4 \
     --public-url https://data.example.com/aoi/chronozarr-4 \
     --update --dry-run
   ```

   Run it again without `--dry-run` to upload.

### Publishing an append

`--update` accepts the destination, `--public-url`, `--profile`, `--endpoint-url`, `--region`, `--apply-cors`, `--workers` and `--dry-run` of a first publish. It rejects `--overwrite`. AWS S3, R2, GCS and Azure support updates. Each adapter can read the hosted root back.

Dry run prints the hosted and local number of timesteps, the objects per phase and the cache headers, and writes nothing. A plain `chronozarr publish` still refuses a prefix that holds other objects and mentions `--update`.

What `--update` writes, in order:

| Phase | Objects | Cache-Control |
|---|---|---|
| 1 | New data chunks of the new timesteps | `public, max-age=31536000, immutable` |
| 2 | `time/c/0` of each level, `volatility` chunks | `public, max-age=300` |
| 3 | Level and array `zarr.json` | `public, max-age=300` |
| 4 | Root `zarr.json` | `public, max-age=300` |

The command refuses a prefix that is not an earlier state of the local store, and a sharded append that rewrites a trailing shard. It deletes nothing. The update is not atomic. [append.md](append.md#publishing) lists what a reader sees between two puts, how a cache that holds the old root behaves and how to resume.

After the upload the command runs `chronozarr doctor` against the public URL and then reads the public root `zarr.json` with `Cache-Control: no-cache`. It prints the link only when the root lists the new number of timesteps. A CDN can still serve the old root for up to 300 seconds. The command then exits with status 1 and no link. The objects are stored, and the same command run later only checks.

For a host without an adapter, use `scripts/upload_stores.sh`. It uploads the files that changed since a stamp file, with the same order and headers.

```bash
STORE=chronozarr-4 ROOT=work scripts/upload_stores.sh --newer-than work/stamp --trailing-ttl 300 aoi-working
```

The script reads `$ROOT/<aoi>/$STORE`. The working copy must sit at `work/<aoi>/chronozarr-4`. Create `work/stamp` with `touch` before the append.

### Cache classes

Only these objects change in place. They need a short lifetime.

| Object | Why it changes | Cache-Control |
|---|---|---|
| root `zarr.json` | new `times`, shapes, `shard_bytes`, consolidated metadata | `public, max-age=300` |
| every other `zarr.json` | new `times`, shapes, `shard_bytes` | `public, max-age=300` |
| `{level}/time/c/0` | the time axis is one chunk | `public, max-age=300` |
| `volatility/c/0/0` | new deltas join the mean of each cell | `public, max-age=300` |
| the shard with the highest time index, for each cell, level and sharded variable (`data`, `mask`, `coverage`) | it gains the new chunks | `public, max-age=300` |
| every other shard | no append rewrites it | `public, max-age=31536000, immutable` |
| every chunk of an unsharded store | new timesteps are new keys | `public, max-age=31536000, immutable` |

`chronozarr publish --update` does not rewrite the trailing-shard row. It refuses a sharded append that changes one (see [append.md](append.md#publishing)).

The trailing shard of a cell is the shard that holds its last timestep. When the next shard opens, the old shard becomes immutable. It keeps `max-age=300`, because `--newer-than` does not touch it. To fix that, upload those shards once without `--newer-than`. A run without `--newer-than` sets the header of every object from the current state of the whole store.

### Cloudflare

The Cache Rule in section 3.2 follows the headers in the table above, so an append needs no rule change. A purge is not needed either: `zarr.json` expires from the edge and from browsers within 300 seconds.

### Open viewers

A viewer keeps the root `zarr.json` it loaded until the page reloads. It shows the old timesteps until then. Reload the page to see the new timesteps. The shared link does not change.

A stale viewer of a sharded store can fail to read a shard index. Reloading fixes it. An unsharded store has no shard index, so it has no such failure. Details: [evidence.md](evidence.md#open-viewers-after-an-append).

### Doctor on an appended store

`chronozarr doctor` passes the same checks on an appended store. Its `cache-control` line reads one object, `0/data/c/0/0/0/0`. In a sharded store with one time shard, that object is the trailing shard. It has `max-age=300`, and doctor warns on a versioned prefix. That warning is expected. Details: [evidence.md](evidence.md#doctor-on-an-appended-store).

Check `zarr.json` with `curl -I` (section 4). For a sharded store, also check a trailing shard.
