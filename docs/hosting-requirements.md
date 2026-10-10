# Hosting requirements and troubleshooting

A chronozarr store is a directory of static files. A host serves it with no server code. The host must return a file by path and send CORS headers. A sharded store also needs byte-range requests.

The requirements come from [spec/CHRONOZARR.md](../spec/CHRONOZARR.md) section 8. Measurements and dates are in [evidence.md](evidence.md).

## Checklist

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

Doctor does not check the requirements below. Verify them with `curl` ([Verifying by hand](#verifying-by-hand)).

| Requirement | Spec level | Why it matters |
|---|---|---|
| An absent key returns `404` | MUST | A missing shard, `mask` or `coverage` array is meaningful. Readers decode a missing shard as fill. A host that answers 200 with HTML breaks that. |
| Chunk and shard responses carry no `Content-Encoding` | MUST | The shard index stores byte offsets into the stored object. A CDN that gzips or re-encodes the body moves the bytes. |
| Shards fit the cacheable object size of the host or CDN (sharded stores) | SHOULD | Choose `shard_time` at encode time ([Shard size](#shard-size)). An unsharded store has one chunk per object. The largest object of the live store is 1.8 MB. |
| Metadata is uploaded last | SHOULD | See [Immutable prefixes and upload order](publish.md#immutable-prefixes-and-upload-order). |
| The prefix is never rewritten | MUST | See [Immutable prefixes and upload order](publish.md#immutable-prefixes-and-upload-order). |

Preflight and range notes:

- A browser sends a preflight only for a suffix range. It sends no preflight for a bounded `bytes=a-b`.
- A store written with `shard_bytes` lets a reader fetch every shard index as a bounded range. Rows 6 and 7 then cost nothing in practice. Doctor still reports them.
- An unsharded store is the encoder default. A reader fetches it with plain `GET` requests and no `Range` header.
- Without `Timing-Allow-Origin`, a benchmark that reads resource timing under-reports bytes. Details: [evidence.md](evidence.md#timing-allow-origin).

## Verifying by hand

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

## Pitfalls

### Compression in front of the store

A CDN, proxy or bucket setting can add `Content-Encoding` to chunk or shard objects. That breaks reads. A reader decodes each chunk with the codecs named in the store metadata and expects the stored bytes unchanged. A shard index holds offsets into the stored bytes, and a `Range` header counts bytes of the encoded response. Turn compression off for the store path, or use content types that the host does not compress.

### Fallback pages

A static host set up for single-page apps answers unknown paths with `200` and `index.html`. The store then seems to have every shard. Serve the store from a host or prefix that returns a real `404`.

### Rewriting a prefix

With `immutable` and a one-year `max-age`, a rewritten object stays stale in browsers and CDNs for up to a year. Always re-encode to a new prefix. The only in-place change is an append, and it changes only the objects in the [cache classes](append.md#cache-classes).

### Local testing

`python -m http.server` ignores `Range`. A sharded store then reads whole shards, and doctor reports a failure. An unsharded store reads fine. For a sharded store, use `chronozarr preview STORE`, `chronozarr.view(store)` from a notebook, or any static server that supports ranges. To show a local store to someone on another machine without a bucket, see [Share a local store instantly](share.md).

### Shard size

A store is unsharded unless you pass `--shard`. With `--shard`, `shard_time` defaults to `n_time`, so one shard holds the whole time axis of a cell. Lower `shard_time` in two cases. First, when a shard would exceed the object size that the host or CDN caches or serves. Second, when a CDN miss on a shard is too slow ([Cloudflare R2](hosting-providers.md#cloudflare-r2)). Keep a store that will grow unsharded ([appending](append.md#appending-to-a-live-store)).
