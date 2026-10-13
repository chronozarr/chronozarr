# Advanced hosting

To put a store online, most people need only [Publish a permanent link](publish.md): `chronozarr publish` uploads the store, checks it and prints the viewer link. The pages below are for setting up a host by hand, for hosts that `publish` does not cover, and for checking and tuning a host.

| I want to | Page |
|---|---|
| Set up Amazon S3 with CloudFront, Cloudflare R2, Google Cloud Storage or Source Cooperative by hand | [Hosting recipes](hosting-providers.md) |
| Upload a store without `chronozarr publish` | [Upload without chronozarr publish](hosting-providers.md#upload-without-chronozarr-publish) |
| Check a host, verify it by hand or fix a failing `doctor` check | [Requirements and checks](hosting-requirements.md) |
| Learn why every version needs a new prefix, and the upload order | [Immutable prefixes and upload order](hosting-requirements.md#immutable-prefixes-and-upload-order) |
| Add timesteps to a store that is already online | [Append timesteps](append.md#appending-to-a-live-store) |
| Keep a store private | [Private stores](private.md) |
| Serve the viewer from my own host | [Self-host the viewer](viewer-distribution.md) |

## What a host must do

A store is a folder of static files. Any host that serves files can serve it, with no server code, if it does these things:

1. It returns each file at its path, and `404` for a file that does not exist.
2. It sends `Access-Control-Allow-Origin: *`, or the address of the viewer, so that the browser may read the files.
3. It returns the stored bytes unchanged. It does not compress them again.
4. For a sharded store, it answers byte-range requests.

A store is unsharded unless you chose `--shard`, so most stores need only the first three. To check a host, run:

```bash
chronozarr doctor https://data.example.org/my_store
```

`doctor` checks each requirement and prints what to change. The [requirements page](hosting-requirements.md#checklist) lists every check. Measurements and dates are in the [measurement log](evidence.md).
