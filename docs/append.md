# Appending to v0.3 stores

`chronozarr append STORE INPUT` adds timesteps at the end of an existing store, in place. The new timesteps must come after the last timestep of the store, on the existing grid. Existing values and coordinates stay fixed.

## Input

`INPUT` is one of these:

- a chronozarr store, for example one month that `chronozarr convert` wrote
- a Zarr store or NetCDF file with dims `(time, band, y, x)`
- a quoted glob of GeoTIFFs, one per timestep, with the date in each file name

The grid, bands, dtype, CRS and nodata of `INPUT` must match `STORE`. For GeoTIFFs this includes how the files mark invalid pixels: the nodata they declare (or none) must be the store's, and they must give a mask (an alpha band, an internal mask or a nodata of NaN) exactly when the store has one. The command checks this before it copies or writes anything, and the message names both values. The command has three options:

| Option | Meaning |
|---|---|
| `--crs` | The CRS of the input. The command checks it against the store. |
| `--variable` | The variable to append from a Zarr or NetCDF input. |
| `--workers` | The number of cells written at the same time. The default is 4. |

## What the append writes

The append writes the objects that gain a timestep, plus the metadata. Every other object keeps its bytes.

- An unsharded store writes new chunk objects. This is the encoder default.
- A sharded store rewrites the shard that receives each new timestep, whole.

For a sharded store that will grow, choose a finite `--shard-time` only when the object count justifies the rewrite cost. Measured costs are in [evidence.md](evidence.md#appending).

## Publishing

In Python package 0.4.0, `chronozarr publish --update` puts an appended store on the prefix that already serves it. The public URL and every shared viewer link stay the same; the store format remains v0.3.

```bash
chronozarr publish my_store \
  --destination s3://my-bucket/aoi/store-v1 \
  --public-url https://data.example.com/aoi/store-v1 \
  --update --dry-run     # drop --dry-run to upload
```

`--update` takes the options of a first `chronozarr publish`, except `--overwrite`. The steps to follow are in [the procedure below](#publishing-an-append). This section states what the command guarantees and what it does not.

### What it does

1. It validates the local store, as `chronozarr validate` does.
2. It lists the prefix and reads the hosted root `zarr.json` from the bucket.
3. It checks that the hosted store is an earlier state of the local store (see Refusals).
4. It uploads the new and changed objects in four phases. A phase starts when the earlier phases are stored.
5. It runs `chronozarr doctor` on the public URL and reads the public root `zarr.json`. The root must list the new number of timesteps.
6. It prints the viewer link of the public URL, only if every check passed.

| Phase | Objects | Cache-Control |
|---|---|---|
| 1 | New data chunks (new keys) | `public, max-age=31536000, immutable` |
| 2 | `time/c/0` of each level and the `volatility` chunks | `public, max-age=300` |
| 3 | Level and array `zarr.json` files | `public, max-age=300` |
| 4 | Root `zarr.json`, last | `public, max-age=300` |

The command skips every object that is stored with the same size and MD5. It deletes nothing and replaces no chunk. `--dry-run` lists the prefix, prints the counts per phase and writes nothing.

### Refusals

The command stops before it writes, and lists every reason, in these cases.

- The hosted timesteps are not the start of the local timesteps. This includes a hosted store that has more timesteps.
- The CRS, bands, nodata value, mask, coverage or the grid of a level differs.
- The prefix holds an object that the local store lacks.
- A hosted chunk or shard has other bytes than the local one.
- The prefix is empty, or holds an upload that stopped before the root `zarr.json`. Use `chronozarr publish` without `--update`.
- The adapter cannot read an object back. The AWS S3, R2, GCS and Azure adapters can.

Sharded stores: an append rewrites the shard that receives the new timestep. A shard is a chunk, and a first `chronozarr publish` marks it `immutable`. Browsers and CDNs can keep it for a year. A new root next to an old shard gives wrong index reads, so `--update` refuses to replace a shard. Publish that store to a new prefix. An append that opens a new shard rewrites none, and `--update` publishes it. Keep a store that will grow unsharded, which is the encoder default.

### What a reader sees

The upload is not atomic. Several objects never change together. One property holds: a chunk or shard that a published root refers to keeps its key and its bytes. Only the objects of phases 2 to 4 are rewritten. The table lists the state between two puts.

| State | A reader that loads the store now |
|---|---|
| Phase 1 | Sees the old store. Nothing refers to the new chunks. |
| Phase 2 | The viewer sees the old store. It takes `times` from the root and never reads `time/c/0` or `volatility`. A Zarr client that holds the old root and reads `time/c/0` gets a chunk that is longer than its metadata. It fails with an error, and opening the store again fixes it. |
| Phase 3 | A reader that uses the consolidated metadata of the root sees the old store. A client that reads each array `zarr.json` can see the new shape next to the old `times`. |
| Root put | One object. A reader gets the old root or the new root. |
| After the root | The new timesteps are readable. Their chunks, time axis and metadata are stored. |

Every chunk of an old timestep exists with the same bytes. A reader that holds an old root can read all of its timesteps.

### Stale metadata

- An open viewer keeps the root that it loaded. It shows the old timesteps, reads them correctly and shows the new ones after a reload.
- A browser cache or CDN that holds the old root serves it for at most `max-age=300` seconds after the change. A page that loads in that time gets the old timesteps, and all of them are readable. After that, the cache asks the origin.
- A cache that holds an old `time/c/0` or level `zarr.json` next to the new root has the same five-minute window. The effects are in the table above.
- Do not request the URL of a new chunk before the update ends. A cache rule that keeps `404` answers would remember the chunk as missing. The command runs `doctor` only after the upload.
- A root that was uploaded by hand with a long `max-age` or `immutable` stays in caches that long. `--update` warns when the public root has such a header. It rewrites every `zarr.json` that it uploads with `max-age=300`.

### Interruption and resume

The root `zarr.json` is the last object written. A run that stops before it leaves the old root in the bucket. A reader that opens the store through the root sees the old store. The new chunks stay in the bucket and nothing refers to them.

To resume, run the same command again. It skips the stored objects and uploads the rest in the same order. A run that stopped in phase 1 uploads only the missing chunks. A run that finished and was not verified uploads nothing and only checks.

If you append again before the rerun, the rerun still works. The hosted store is still an earlier state of the local store.

If the final check reports the old number of timesteps, a cache still holds the old root. The objects are stored. Wait five minutes or purge `zarr.json` at the CDN, then run the same command again.

### Two publishers

There is no lock. Two runs that list the prefix before either writes both continue, and the later put wins. The command does not detect this. Run one publisher for each prefix. A run that lists the prefix after the other ended compares against the new state.

## Appending to a live store

### The live store

The demo catalog lists `ucayali_santa_maria_v03` and `ucayali_santa_maria/png-v03`. R2 serves them with `deploy/r2-cors.json` and the [Cache Rule](hosting-providers.md#cache-rule). The history of its prefixes and its doctor results are in [evidence.md](evidence.md#live-store-and-publishing).

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

The command refuses a prefix that is not an earlier state of the local store, and a sharded append that rewrites a trailing shard. It deletes nothing. The update is not atomic. [append.md](#publishing) lists what a reader sees between two puts, how a cache that holds the old root behaves and how to resume.

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

`chronozarr publish --update` does not rewrite the trailing-shard row. It refuses a sharded append that changes one (see [append.md](#publishing)).

The trailing shard of a cell is the shard that holds its last timestep. When the next shard opens, the old shard becomes immutable. It keeps `max-age=300`, because `--newer-than` does not touch it. To fix that, upload those shards once without `--newer-than`. A run without `--newer-than` sets the header of every object from the current state of the whole store.

### Cloudflare

The [Cache Rule](hosting-providers.md#cache-rule) follows the headers in the table above, so an append needs no rule change. A purge is not needed either: `zarr.json` expires from the edge and from browsers within 300 seconds.

### Open viewers

A viewer keeps the root `zarr.json` it loaded until the page reloads. It shows the old timesteps until then. Reload the page to see the new timesteps. The shared link does not change.

A stale viewer of a sharded store can fail to read a shard index. Reloading fixes it. An unsharded store has no shard index, so it has no such failure. Details: [evidence.md](evidence.md#open-viewers-after-an-append).

### Doctor on an appended store

`chronozarr doctor` passes the same checks on an appended store. Its `cache-control` line reads one object, `0/data/c/0/0/0/0`. In a sharded store with one time shard, that object is the trailing shard. It has `max-age=300`, and doctor warns on a versioned prefix. That warning is expected. Details: [evidence.md](evidence.md#doctor-on-an-appended-store).

Check `zarr.json` with `curl -I` ([Verifying by hand](hosting-requirements.md#verifying-by-hand)). For a sharded store, also check a trailing shard.
