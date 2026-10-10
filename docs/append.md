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

`chronozarr share` is for a temporary laptop-hosted review and stops when its command stops. Use `publish --update` when a growing store needs a durable URL.

```bash
chronozarr publish my_store \
  --destination s3://my-bucket/aoi/store-v1 \
  --public-url https://data.example.com/aoi/store-v1 \
  --update --dry-run     # drop --dry-run to upload
```

`--update` takes the options of a first `chronozarr publish`, except `--overwrite`. The steps to follow are in [hosting.md](hosting.md#publishing-an-append). This section states what the command guarantees and what it does not.

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
