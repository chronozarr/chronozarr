# Hosting recipes

Each recipe sets up one host for a chronozarr store: bucket, CORS, caching and the `put` function for the [upload script](publish.md#the-upload-script). `chronozarr publish` runs the upload for S3, R2, GCS and Azure, see [publish.md](publish.md#chronozarr-publish).

## Amazon S3 with CloudFront

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

5. Upload with the [upload script](publish.md#the-upload-script). Define `put` as:

   ```bash
   put() { aws s3 cp "$1" "s3://$BUCKET/$PREFIX/${1#./}" --cache-control "$CC" --only-show-errors; }
   ```

The store URL is `https://<distribution-domain>/$PREFIX`.

Notes:

- The upload stores `Cache-Control` on each object. CloudFront passes it through. You do not invalidate anything, because prefixes are immutable.
- `aws s3 cp` labels extensionless objects `binary/octet-stream`. CloudFront compresses only a fixed list of content types, for objects between 1,000 and 10,000,000 bytes. `binary/octet-stream` is not on the list. Shards are served as stored. Confirm this with [Verifying by hand](hosting-requirements.md#verifying-by-hand).
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

## Cloudflare R2

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

2. Upload with the [upload script](publish.md#the-upload-script). Define `put` as:

   ```bash
   put() { npx --no-install wrangler r2 object put "$BUCKET/$PREFIX/${1#./}" --file "$1" --cache-control "$CC" --remote > /dev/null; }
   ```

3. Create a Cache Rule and a Transform Rule on the hostname. Both are in the dashboard under Rules. Both need zone write access. The `wrangler login` token used for deploys does not have it.

Wrangler needs about 2 seconds per object. An unsharded store has thousands of objects. For the first upload of an unsharded store, use an S3 client against the S3 endpoint of R2. Each later append uploads only the objects it wrote ([appending](append.md#appending-to-a-live-store)). Upload times are in [evidence.md](evidence.md#live-store-and-publishing).

### Cache Rule

Cloudflare does not cache objects without a file extension by default. Chunk keys have none, so their responses stay `cf-cache-status: DYNAMIC` until you add a Cache Rule.

Create the rule in the dashboard under Caching, then Cache Rules:

1. Set the field to `Hostname`, the operator to `equals`, and the value to your data hostname. The expression is `(http.host eq "data.example.com")`.
2. Set Cache eligibility to Eligible for cache.
3. Set Edge TTL to Use cache-control header if present, bypass cache if not.
4. Under Caching, then Configuration, set Browser Cache TTL to Respect Existing Headers.

The upload script sets `Cache-Control` on every object (the [cache classes](append.md#cache-classes)). The rule follows those headers. Chunks stay cached for a year, and `zarr.json` expires after 300 seconds, so an append shows up within five minutes.

Step 3 also keeps `404` responses out of the cache. A missing chunk means fill, and an append later creates it.

Step 4 matters. The zone default raises any shorter browser lifetime to four hours, so `zarr.json` would reach browsers with `max-age=14400`.

Do not match on a URI Full wildcard. That expression matches nothing, and every response stays `DYNAMIC`. The dashboard warns that a hostname rule "may not apply to your traffic" for an R2 hostname. The warning is wrong for this case. The verification is in [evidence.md](evidence.md#cloudflare-cache-rule).

### `Timing-Allow-Origin`

Add a Transform Rule of type Modify Response Header. Use the same hostname expression. Set the action to Set static, with `Timing-Allow-Origin` = `*`.

### Object size and miss cost

A cache miss on a range read pulls the whole object from R2, so the cost grows with object size. In the live unsharded store the largest chunk object is 1.8 MB, and a miss is cheap. In a sharded store, a smaller `shard_time` makes a miss cheaper. Cloudflare limits the size of a cacheable object by plan. Measurements are in [evidence.md](evidence.md#layout-choice).

### One cached copy per requesting site

With a CORS policy, R2 sends `Vary: Origin`, so Cloudflare caches a separate copy of each object for each site that requests it. Visitors of one site share a cache. A site that serves the reader or the viewer from its own origin starts with a cache miss on every object. So does a client that sends no `Origin` header (Python, GDAL, curl). An iframe of the hosted viewer sends `Origin: https://chronozarr.org`. A Transform Rule that removes `Vary` does not merge the copies. Measurements are in [evidence.md](evidence.md#cache-copies-per-requesting-site).

### `r2.dev`

The public development URL (`wrangler r2 bucket dev-url enable`) is rate limited. Use it for a first check, and use a custom domain for anything public.

## Google Cloud Storage

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

2. Upload with the [upload script](publish.md#the-upload-script). Define `put` as:

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

## Source Cooperative

[Source Cooperative](https://source.coop) (Radiant Earth) hosts open datasets. It serves them through a data proxy at `https://data.source.coop`. An object has the address `https://data.source.coop/<account>/<product>/<path>`.

You do not configure CORS or cache headers on this host. The proxy is in beta. The storage behind it (S3, GCS, Azure or R2) is not your choice.

Expect `chronozarr doctor` to pass checks 1 to 8 and 12. It warns on `cache-control` for a versioned prefix. It reports `edge cache` and `timing-allow-origin` as info. Observations: [evidence.md](evidence.md#source-cooperative).

You need an account and upload access for the product. If the product page shows no upload option, write to hello@source.coop.

Choose one of two upload routes.

### Route 1: the web UI

1. Open the product page.
2. Click the lock icon, then Edit Mode.
3. Drag in the level directories (`0/`, `1/`, ...), and `volatility/` if the store has one.
4. Drag in the root `zarr.json` by itself, last.

The UI does not order uploads, so step 4 is a separate action. A sharded store (`--shard`) has about a hundred objects, which is workable here. An unsharded store has thousands of objects, which is not.

### Route 2: an S3 client through the proxy

1. Run `source-coop login` to get temporary credentials.
2. Create an AWS profile named `source-coop`, as the Source docs describe.
3. Upload with the [upload script](publish.md#the-upload-script). Define `put` as:

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
