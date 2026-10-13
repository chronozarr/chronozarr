# Publish a permanent link

`chronozarr publish` copies a store to cloud storage in your own account, checks that a browser can read it, and prints a viewer link. The link keeps working when your computer is off.

You need four things:

1. A bucket, or an Azure container. The command does not create one.
2. Credentials that can write to it.
3. A public address for it: the bucket itself, readable by anyone, or a CDN or custom domain in front of it.
4. A CORS rule that lets the viewer page read it. `--apply-cors` adds the rule.

Anyone who has the link can read the store. Your provider charges you for the storage and for each download, and every view downloads part of the store. chronozarr.org serves the viewer page and stores no data. For data that must stay private, see [Private stores](private.md). For a link that lasts only while your computer is on, see [Share a temporary link](share.md).

## Choose a provider

| Provider | Destination | Extra | Public address | Set up the bucket |
|---|---|---|---|---|
| Amazon S3 | `s3://BUCKET/PREFIX` | `publish` | `https://BUCKET.s3.REGION.amazonaws.com/PREFIX`, or a CloudFront address | [Public bucket](hosting-providers.md#amazon-s3-public-bucket) or [with CloudFront](hosting-providers.md#amazon-s3-with-cloudfront) |
| Cloudflare R2 | `s3://BUCKET/PREFIX` and `--endpoint-url` | `publish` | A custom domain, or the bucket's `r2.dev` address. Always given as `--public-url` | [Cloudflare R2](hosting-providers.md#cloudflare-r2) |
| Google Cloud Storage | `gs://BUCKET/PREFIX` | `publish-gcs` | `https://storage.googleapis.com/BUCKET/PREFIX` | [Google Cloud Storage](hosting-providers.md#google-cloud-storage) |
| Azure Blob Storage | `az://ACCOUNT/CONTAINER/PREFIX` | `publish-azure` | `https://ACCOUNT.blob.core.windows.net/CONTAINER/PREFIX` | [Azure Blob Storage](hosting-providers.md#azure-blob-storage) |

For another static host, such as Source Cooperative or a university web server, see [Hosting recipes](hosting-providers.md).

## Publish

1. Install the extra for your provider. With `uv tool install`, list every extra that you use:

   ```bash
   uv tool install --python 3.13 'chronozarr[geo,publish]==0.4.1'
   ```

2. Sign in to the provider. The command uses the provider's usual credentials:

   | Provider | Credentials |
   |---|---|
   | Amazon S3 | `aws configure`, `aws sso login`, the `AWS_*` variables or an instance role. `--profile` picks a profile |
   | Cloudflare R2 | An R2 API token as `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`, or a profile. A token with admin permission can also set CORS |
   | Google Cloud Storage | `gcloud auth application-default login`, or `GOOGLE_APPLICATION_CREDENTIALS` |
   | Azure Blob Storage | `az login`, a service principal in `AZURE_CLIENT_ID`, `AZURE_TENANT_ID` and `AZURE_CLIENT_SECRET`, or `AZURE_STORAGE_CONNECTION_STRING` |

3. Make the bucket readable. Follow the recipe for your provider in the table above. `publish` never changes who can read a bucket.

4. Check the plan. A dry run prints what would be uploaded and what the prefix holds, and uploads nothing:

   ```bash
   chronozarr publish my_store --destination s3://my-bucket/my_store-v1 --apply-cors --dry-run
   ```

5. Publish:

   ```bash
   chronozarr publish my_store --destination s3://my-bucket/my_store-v1 --apply-cors
   ```

Use a new prefix, such as `my_store-v2`, for every new version of a store. A prefix that holds other files is refused.

## What you see

The command prints the plan, uploads, checks the public address as a browser would, and prints the link. A shortened run:

```text
store        my_store (conforms to chronozarr 0.3.0)
destination  s3://my-bucket/my_store-v1 via AWS S3, region us-east-1
public URL   https://my-bucket.s3.us-east-1.amazonaws.com/my_store-v1 (derived from the AWS S3 endpoint; pass --public-url for a CDN or custom domain; checked after the upload)
objects      51 objects, 46,149,689 bytes
...
chronozarr doctor https://my-bucket.s3.us-east-1.amazonaws.com/my_store-v1
[ ok ] root zarr.json                  200, 17,214 bytes in 5 ms, chronozarr 0.3.0
[ ok ] CORS on zarr.json               Access-Control-Allow-Origin allows https://chronozarr.org
...
[ ok ] decode level 0                  cell (1,1) t=0 and t=5 in 27 ms; plain-Zarr pixel at t=0 matches
11 ok, 3 info, 0 warning(s), 0 failure(s)

added the viewer CORS rule after the bucket's 0 existing rule(s)

verified. Open the store in the viewer:

  https://chronozarr.org/demo/?store=https%3A%2F%2Fmy-bucket.s3.us-east-1.amazonaws.com%2Fmy_store-v1
```

Open the link, choose the view that you want to show, and click Copy link. The copied link also holds the date, the product and the map view. `chronozarr link URL` prints a link with a chosen product and date from the command line.

## Provider examples

```bash
# Amazon S3 behind CloudFront: the bucket stays private, so give the distribution's address.
chronozarr publish my_store \
  --destination s3://my-bucket/aoi/store-v1 \
  --public-url https://d111111abcdef8.cloudfront.net/aoi/store-v1 \
  --profile research

# Cloudflare R2. The endpoint is for uploads only, so --public-url is required.
chronozarr publish my_store \
  --destination s3://my-bucket/aoi/store-v1 \
  --endpoint-url https://<account id>.r2.cloudflarestorage.com \
  --public-url https://data.example.com/aoi/store-v1

# Google Cloud Storage. The public address is https://storage.googleapis.com/my-bucket/aoi/store-v1.
chronozarr publish my_store --destination gs://my-bucket/aoi/store-v1 --apply-cors

# Azure Blob Storage: account, container, prefix.
chronozarr publish my_store --destination az://myaccount/stores/aoi/store-v1 --apply-cors
```

`--profile`, `--endpoint-url` and `--region` are S3 options. The command rejects them with a `gs://` or `az://` destination.

## The destination and the public URL

A published store has two addresses:

| | Destination | Public URL |
|---|---|---|
| Form | `s3://BUCKET/PREFIX`, `gs://BUCKET/PREFIX` or `az://ACCOUNT/CONTAINER/PREFIX` | `https://HOST/PREFIX` |
| Used by | the upload, with your credentials | browsers and the viewer link |
| Source | `--destination` | `--public-url`, or the bucket's own HTTPS address |

The bucket's own address serves objects only when the bucket allows anonymous reads. For a private bucket behind a CDN, pass the CDN's address as `--public-url`. R2 has no default: its storage endpoint needs credentials, so give the custom domain or the `r2.dev` address. The command never uses `storage.cloud.google.com`, because that host needs a Google sign-in and does not answer CORS.

A public URL that is an `s3://` address, or that holds a login, a signed query or a fragment, is refused. It ends up in a link that other people open.

## When a check fails

The objects stay in the bucket, no link is printed, and the exit status is 1. Fix the cause and run the same command again: it skips the objects already stored.

| What the checks say | Cause and fix |
|---|---|
| `root zarr.json ... HTTP 403` | The bucket is not readable by anyone. Follow the recipe for your provider, or pass a CDN address as `--public-url` |
| `root zarr.json ... HTTP 404` | The public URL does not point at the store. It must be the folder that holds the root `zarr.json` |
| `CORS on zarr.json` and two more CORS lines fail | The bucket has no CORS rule for the viewer. Add `--apply-cors`. Behind a CDN, set CORS on the CDN |
| `validate` fails with HTTP 403 | The host answers `403` for a file that does not exist. On S3, grant `s3:ListBucket` as in the recipes, so it answers `404` |
| `no credentials found`, `Unable to locate credentials`, or access denied | The credentials are missing or cannot write to the bucket. See step 2 |
| `bucket ... was not found` | Create the bucket first, or check its name and `--endpoint-url` |
| `--public-url is required` | The destination is an R2 or other S3-compatible endpoint. Give the custom domain or `r2.dev` address |

`chronozarr doctor URL` runs the same checks at any time. [Requirements and checks](hosting-requirements.md#checklist) explains each one.

## What the command does

1. Validates the store, as `chronozarr validate` does. A store that fails is not uploaded.
2. Prints the plan: destination, public URL, object count, bytes, the four upload phases and the cache headers.
3. Lists the prefix. A prefix that holds only objects identical to the store (same size and MD5) is an interrupted upload, and the command resumes it. A prefix that holds any other object is refused. A store that you published and then appended to is the exception: use `--update` ([Publishing an append](append.md#publishing-an-append)). `--overwrite` replaces objects whose content differs. It never deletes, and it leaves a one-year cached copy stale for readers that already loaded it.
4. Uploads in four phases: data chunks, then the time and volatility chunks, then the group and array `zarr.json` files, then the root `zarr.json`. A phase starts after the previous one is fully stored. If an upload fails, the command stops with no root `zarr.json` in the bucket and names the failing key. boto3 and the Azure client retry each request up to five times. The Google client retries transient errors with its default policy.
5. Runs the doctor checks against the public URL, with `Origin: https://chronozarr.org`.
6. Prints `https://chronozarr.org/demo/?store=<URL-encoded public URL>` only when no check failed. Warnings are printed and do not block the link.

Cache headers follow the [cache classes](append.md#cache-classes). Every `zarr.json`, each level's `time/c/0` and the `volatility` chunks get `public, max-age=300`. Every other object gets `public, max-age=31536000, immutable`. The command does not mark the last shard of a sharded store as short-lived, so `--update` refuses a sharded append that rewrites a trailing shard.

## CORS and public access

- The command reads the bucket's CORS rules only when the doctor reports a CORS failure. Without `--apply-cors` it prints the rule the viewer needs and the number of rules that exist, and changes nothing.
- With `--apply-cors` it writes the existing rules unchanged, in their order, followed by the viewer rule, and runs the checks again. Putting the new rule last means that no request an existing rule answers today changes its answer. If an earlier rule already matches the viewer's requests without the headers it needs, the checks still fail, and you merge the rules by hand.
- Google Cloud Storage keeps CORS on the bucket as a list of rules. The viewer rule is the one in [Google Cloud Storage](hosting-providers.md#google-cloud-storage).
- Azure keeps CORS on the Blob service of the storage account. One rule list applies to every container in the account. Azure allows five rules, and the command refuses to write a sixth. Reading and writing the list needs the Storage Account Contributor role, or an account key in `AZURE_STORAGE_CONNECTION_STRING`.
- S3 CORS needs `s3:GetBucketCORS` and `s3:PutBucketCORS`. An R2 token with Object Read and Write cannot manage CORS. Use a token with admin permission, or set the rule with wrangler ([Cloudflare R2](hosting-providers.md#cloudflare-r2)).
- Behind CloudFront, CORS comes from the response headers policy ([Amazon S3 with CloudFront](hosting-providers.md#amazon-s3-with-cloudfront)). The bucket's CORS rules are not what the checks see.
- The command never changes a bucket's public access settings, bucket policy, IAM bindings, custom domains or cache rules. Setting CORS does not make private objects readable.

## Next

- [Append timesteps](append.md) to a published store, then `publish --update`.
- [Embed the viewer](embedding.md) in your own page.
- [Advanced hosting](hosting.md): manual uploads, other hosts and the requirements behind each check.
