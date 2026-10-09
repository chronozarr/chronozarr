# Appending to v0.3 stores

`chronozarr append STORE INPUT` adds timesteps at the end of an existing store, in place. The new timesteps must come after the last timestep of the store, on the existing grid. Existing values and coordinates stay fixed.

## Input

`INPUT` is one of these:

- a chronozarr store, for example one month that `chronozarr convert` wrote
- a Zarr store or NetCDF file with dims `(time, band, y, x)`
- a quoted glob of GeoTIFFs, one per timestep, with the date in each file name

The grid, bands, dtype, CRS and nodata of `INPUT` must match `STORE`. For GeoTIFFs, a band matches when its name, scale, offset and units match. A GeoTIFF that sets no scale or offset has scale 1 and offset 0, so it does not match a store whose bands are scaled. A nodata value that the GeoTIFFs declare must be the store's. The command has three options:

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

An append is not atomic. Run it on a working copy and check the copy with `chronozarr validate` before you publish.

Publish the data objects first and the root `zarr.json` last. The objects that an append changes carry `max-age=300`, so cached copies expire within five minutes. [hosting.md](hosting.md#7-appending-to-a-live-store) has the procedure and the cache lifetimes.
