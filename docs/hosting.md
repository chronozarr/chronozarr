# Hosting a chronozarr store

A chronozarr store is a directory of static files. A host serves it with no server code. The host must return a file by path and send CORS headers. A sharded store also needs byte-range requests.

The pages below publish a store that anyone with its URL can read. For data that must stay private, see [private.md](private.md). Measurements and dates are in [evidence.md](evidence.md).

| I want to | Page |
|---|---|
| Upload a store and get a viewer link | [publish.md](publish.md) |
| Set up S3 with CloudFront, R2, GCS or Source Cooperative | [hosting-providers.md](hosting-providers.md) |
| Check a host against the requirements, verify by hand or fix a failing check | [hosting-requirements.md](hosting-requirements.md) |
| Add timesteps to a store that is already live | [append.md](append.md#appending-to-a-live-store) |
| Keep a store private | [private.md](private.md) |
| Share a store with a link or embed it | [share.md](share.md) |
