# Publish a store

`chronozarr publish STORE --destination ...` uploads a store to S3, R2, GCS or Azure, checks the hosted store with `chronozarr doctor` and prints a viewer link. The data stays in your bucket.

The command publishes a store that anyone with its URL can read. For data that must stay private, see [private.md](private.md). For a host-specific setup, see [hosting-providers.md](hosting-providers.md). For the requirements a host must meet, see [hosting-requirements.md](hosting-requirements.md).

## `chronozarr publish`

`chronozarr publish STORE` is available in the released Python package 0.4.0. It runs the four phases below against an S3 bucket, an S3-compatible bucket such as Cloudflare R2, a Google Cloud Storage bucket or an Azure Blob Storage container. It then checks the hosted store with `chronozarr doctor` and prints a viewer link. The destination scheme picks the provider:

| Provider | Destination | Extra | SDK | Credentials |
|---|---|---|---|---|
| AWS S3, Cloudflare R2 | `s3://BUCKET/PREFIX` | `publish` | boto3 | boto3's chain |
| Google Cloud Storage | `gs://BUCKET/PREFIX` | `publish-gcs` | google-cloud-storage | Application Default Credentials |
| Azure Blob Storage | `az://ACCOUNT/CONTAINER/PREFIX` | `publish-azure` | azure-storage-blob, azure-identity | `DefaultAzureCredential`, or `AZURE_STORAGE_CONNECTION_STRING` |

Each provider has its own extra, so you install one SDK only. For example, use
`uv add 'chronozarr[publish-azure]==0.4.0'` in a project, or
`uv tool install 'chronozarr[publish-azure]==0.4.0'` for the standalone CLI.
The pip equivalent is `pip install 'chronozarr[publish-azure]==0.4.0'`.
Package 0.4.0 still writes the v0.3 store format.

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

An `s3://` address is rejected as a public URL, and so is a URL with credentials, a query string (a signed URL) or a fragment, because the URL ends up in a link that other people open. For R2 there is no default: the storage endpoint is authenticated, so give the custom domain or the public endpoint of the bucket. For AWS the default `https://BUCKET.s3.REGION.amazonaws.com/PREFIX` serves objects only when the bucket allows public reads. For a private bucket behind CloudFront ([Amazon S3 with CloudFront](hosting-providers.md#amazon-s3-with-cloudfront)), pass the distribution URL. For Google Cloud Storage the default is `https://storage.googleapis.com/BUCKET/PREFIX`. It serves objects only when the bucket allows anonymous reads. The command never uses `storage.cloud.google.com`, because that host authenticates with cookies and does not answer CORS. For Azure the default is `https://ACCOUNT.blob.core.windows.net/CONTAINER/PREFIX`. It serves objects only when the storage account allows anonymous access and the container's public access level is Blob.

The Azure destination names the storage account because a container name alone does not say which account to write to. An `https://ACCOUNT.blob.core.windows.net/...` address is not accepted as a destination. Pass it as `--public-url`.


What the command does, in order:

1. Validates the store, as `chronozarr validate` does. A store that fails is not uploaded.
2. Prints the plan: destination, public URL, object count, bytes, the four phases and the cache headers.
3. Lists the prefix. A prefix that holds only objects identical to the store (same size and MD5) is an interrupted upload and resumes. A prefix that holds any other object is refused. Use a new prefix for every version. A store that you published and then appended to is the exception: use `--update` ([Publishing an append](append.md#publishing-an-append)). `--overwrite` replaces objects whose content differs. It never deletes, and it leaves a one-year cached copy stale for readers that already loaded it.
4. Uploads in four phases: data chunks, then the time and volatility chunks, then the group and array `zarr.json` files, then the root `zarr.json`. A phase starts after the previous one is fully stored. If an upload fails, the command stops with no root `zarr.json` in the bucket and names the failing key. Running the same command again skips the stored objects, so a retry repeats no work and a finished upload is a no-op. boto3 retries each request up to five times before a failure is reported. The Google client retries transient errors with its default policy, which the command requests explicitly for every upload. The Azure client retries each request up to five times.
5. Runs the doctor checks against the public URL, with `Origin: https://chronozarr.org`.
6. Prints `https://chronozarr.org/demo/?store=<URL-encoded public URL>` only when no check failed. Warnings are printed and do not block the link. When a check fails, the objects stay in the bucket, no link is printed and the exit status is 1.

Cache headers follow the [cache classes](append.md#cache-classes). Every `zarr.json`, each level's `time/c/0` and the `volatility` chunks get `public, max-age=300`. Every other object gets `public, max-age=31536000, immutable`. The command does not mark the last shard of a sharded store as short-lived. `--update` therefore refuses a sharded append that rewrites a trailing shard ([cache classes](append.md#cache-classes)).

CORS and public access:

- The command reads the bucket's CORS rules only when the doctor reports a CORS failure. Without `--apply-cors` it prints the rule the viewer needs and the number of rules that exist, and changes nothing.
- With `--apply-cors` it writes the existing rules unchanged, in their order, followed by the viewer rule (the rule of `deploy/r2-cors.json`), and runs the checks again. Putting the new rule last means no request that an existing rule answers today changes its answer. If an earlier rule already matches the viewer's requests without the headers it needs, the checks still fail and you merge the rules by hand.
- Google Cloud Storage keeps CORS on the bucket as a list of rules. The command reads it with `storage.buckets.get` and writes it with `storage.buckets.update`. The viewer rule is the one in [Google Cloud Storage](hosting-providers.md#google-cloud-storage).
- Azure keeps CORS on the Blob service of the storage account. One rule list applies to every container in the account. The command keeps the existing rules, appends the viewer rule and writes the whole list. Azure allows five rules, and the command refuses to write a sixth. Reading and writing the list needs the Storage Account Contributor role (Microsoft.Storage/storageAccounts/blobServices/read and write), or an account key in `AZURE_STORAGE_CONNECTION_STRING`.
- S3 CORS needs `s3:GetBucketCORS` and `s3:PutBucketCORS`. An R2 token with Object Read and Write cannot manage CORS. Use a token with bucket admin permission, or run `npx wrangler r2 bucket cors set` ([Cloudflare R2](hosting-providers.md#cloudflare-r2)).
- When a bucket sits behind CloudFront, CORS comes from the response headers policy ([Amazon S3 with CloudFront](hosting-providers.md#amazon-s3-with-cloudfront)), and the bucket's CORS rules are not what the checks see.
- The command never changes a bucket's public access block, bucket policy, IAM bindings, custom domains or cache rules. For Google Cloud Storage that includes `allUsers` grants and public access prevention. For Azure it includes the account's anonymous access setting and the container's public access level. When the objects are stored but the doctor gets 403 or 404, it prints what to change in the provider's console. Setting CORS does not make private objects readable.

The command does not create buckets. The dataset stays in your bucket, and its storage and delivery charges are yours. chronozarr.org serves the viewer and holds no data.

## Immutable prefixes and upload order

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

### The upload script

The three phases work for every host. The script below runs them.

1. Save the script as `upload.sh`.
2. Define `put` from the [recipe for your host](hosting-providers.md).
3. Run `bash upload.sh`.

`put FILE` uploads `./FILE`, relative to the store directory, to `$PREFIX/FILE` with `Cache-Control: $CC`.

```bash
#!/usr/bin/env bash
set -euo pipefail
STORE=data/stores/my_aoi/chronozarr-2   # local store directory
PREFIX=my_aoi/chronozarr-2              # new for every encode
CC="public, max-age=31536000, immutable"

# put() goes here, from the [recipe for your host](hosting-providers.md)

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
- The objects that an append rewrites get `max-age=300`. The [cache classes](append.md#cache-classes) list them.
- `--trailing-ttl SECONDS` changes the 300.
- `--dry-run` prints the class of every object and uploads nothing.
