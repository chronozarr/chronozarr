# chronozarr v0.1

**Status:** Draft
**Date:** 2026-09-29
**Spec version string:** `0.1.0`
**Supersedes:** `CHRONOFABRIC.md` (ChronoFabric v1: Zarr v2, one array per cell, `manifest.json`)

## 0. Purpose

chronozarr is a Zarr v3 layout convention for raster time series (satellite image stacks). A browser opens a decade of analysis-ready data from a static bucket, scrubs through time like video, and returns the exact stored value on click. It is a **convention plus a reader**. It is not a codec and not a new container: every object in a chronozarr store is a plain Zarr v3 object, readable by any Zarr v3 library.

The temporal encoding (star-delta, §4) cannot be a per-chunk Zarr codec: decoding a delta timestep needs the anchor chunk, which is a different chunk. A chronozarr-aware reader is therefore required for non-anchor timesteps. A plain Zarr reader (`xarray.open_zarr`, zarrita) sees anchor timesteps correctly and non-anchor timesteps as int16 residuals reinterpreted as uint16. This is the documented cost of the convention.

MUST, MUST NOT, SHOULD, MAY are used as in RFC 2119.

## 1. Invariants

1. **Lossless roundtrip.** `encode(data)` then `open()` returns the exact input uint16 values. No quantization, no rounding, no lossy compression.
2. **O(1) random timestep access.** Any timestep at any level decodes with at most two chunk reads: one anchor plus one delta. No chain accumulation, no sequential dependency.
3. **uint16 preservation.** Values are stored and served as unsigned 16-bit integers. No conversion to float or 8-bit anywhere in the storage or serving path.
4. **Self-describing.** Group and array attributes fully describe layout, temporal encoding, CRS and chunk grid. No sidecar files, no external metadata.

## 2. Store layout

A chronozarr store is a Zarr v3 hierarchy. Zarr v3 arrays are leaf nodes, so each LOD level is a **group** named `"0"`, `"1"`, ... holding one data array (default name `data`, declared in `chronozarr.variable`) plus its coordinate arrays. This is the shape ndpyramid writes and carbonplan/zarr-layer reads (`{store}/{level}/{variable}`).

```
{store}/
  zarr.json                      root group; attrs: multiscales, chronozarr                (§3.1)
  volatility/
    zarr.json                    array (grid_rows_0, grid_cols_0) float32                  (§5)
    c/0/0
  0/
    zarr.json                    level group; attrs: crs, transform, resolution            (§3.3)
    data/
      zarr.json                  array (time, band, y, x) uint16, chunks (1, n_band, cs, cs)
      c/{t}/0/{r}/{c}            one object per (timestep, cell)   [unsharded; sharded form §7]
    time/  zarr.json  c/0        int64 ms since epoch, length n_time
    band/  zarr.json  c/0        band names, length n_band
    y/     zarr.json  c/0        float64 projected y of pixel centres, length H_0
    x/     zarr.json  c/0        float64 projected x of pixel centres, length W_0
  1/
    ...                          same members; H_1 = ceil(H_0 / 2), W_1 = ceil(W_0 / 2)
  2/ ...                         consecutive levels until the grid is 1x1                  (§6)
```

### 2.1 Data array `{level}/{variable}`

| Field | Value |
|---|---|
| `shape` | `[n_time, n_band, H_k, W_k]` |
| `data_type` | `uint16` |
| `chunk_grid` | `regular`, `chunk_shape = [1, n_band, cs, cs]` (unsharded) |
| `chunk_key_encoding` | `default`, separator `/`, so keys are `c/{t}/0/{r}/{c}` |
| `fill_value` | MUST equal `nodata` (0) |
| `codecs` | `bytes` (`endian: little`) then one compression codec (§8) |
| `dimension_names` | `["time", "band", "y", "x"]` |
| `attributes` | `_ARRAY_DIMENSIONS: ["time","band","y","x"]`, `nodata: 0` |

- `cs` (chunk size) MUST be identical at every level. Default 512. 256 is permitted for low-latency stores.
- The band chunk index is always 0: one chunk holds every band of one timestep of one cell.
- Cell `(r, c)` at level `k` is array slice `[:, :, r*cs:(r+1)*cs, c*cs:(c+1)*cs]`; row 0 is the top (north) edge. `grid_rows_k = ceil(H_k / cs)`, `grid_cols_k = ceil(W_k / cs)`.
- **Edge chunks** are ordinary Zarr chunks: stored at full `cs x cs`; elements outside `shape` equal `fill_value`. Readers MUST discard elements beyond `shape`. There is no edge-size formula and no per-chunk size metadata. Padding is 0 in anchors and 0 in deltas, so it costs nothing after compression.

### 2.2 Coordinate arrays `{level}/{time,band,y,x}`

| Array | `data_type` | Contents |
|---|---|---|
| `time` | `int64` | Milliseconds since Unix epoch. Attrs `units: "milliseconds since 1970-01-01T00:00:00"`, `calendar: "proleptic_gregorian"` (CF), so xarray decodes to `datetime64` natively. MUST be strictly increasing. Any cadence: monthly, weekly, irregular. |
| `band` | `string` (codec `vlen-utf8`) or `int32` | Band names in array order where the writer supports Zarr v3 strings; otherwise the band index `0..n_band-1`. |
| `y` | `float64` | Projected y of pixel centres in `crs`, decreasing (north up). |
| `x` | `float64` | Projected x of pixel centres in `crs`, increasing. |

- Every array in the store MUST set `dimension_names` and the attribute `_ARRAY_DIMENSIONS` to the same list (`dimension_names` is native Zarr v3; `_ARRAY_DIMENSIONS` is what xarray-v2-era tools read).
- Each coordinate array SHOULD be a single chunk. `time` and `band` MUST be identical at every level.
- The same timestamps as ISO-8601 strings and the band names are duplicated in root attrs `chronozarr.times` and `chronozarr.bands` (§3.2), so a JS reader never needs int64 or vlen strings. `chronozarr.times[i]` MUST be the ISO-8601 rendering of `time[i]`.

## 3. Attributes

### 3.1 Root group

| Key | Type | Rule |
|---|---|---|
| `multiscales` | list | ndpyramid schema (§3.4). Exactly one entry. |
| `chronozarr` | object | §3.2. |

Writers SHOULD also write zarr-python 3 `consolidated_metadata` into the root `zarr.json` so a reader learns every array's shape and codecs in one GET. Readers MUST fall back to per-array `zarr.json` when it is absent.

### 3.2 `chronozarr` block

Complete instance in the root example, §3.5.

| Field | Type | Rule |
|---|---|---|
| `spec_version` | string | `0.1.x`. Readers MUST reject other versions. |
| `variable` | string | Name of the data array inside each level group. Default `"data"`. Readers MUST NOT hardcode it. |
| `times` | string[] | ISO-8601 timestamp per timestep, length `n_time`, equal to the `time` coordinate. |
| `bands` | string[] | Band names in array order, length `n_band`. |
| `nodata` | int | 0 in v0.1. MUST equal every data array's `fill_value` and `nodata` attr. |
| `crs` | string | `EPSG:<code>`. MUST equal every level's `crs` and every `multiscales.datasets[].crs`. A projected per-AOI CRS (UTM) SHOULD be used. Web Mercator SHOULD NOT: its pixels are not equal-area, so block means and statistics are biased. |
| `temporal.encoding` | string | `"star-delta"` only. |
| `temporal.anchor_interval` | int >= 1 | Timesteps between anchors. `1` means every timestep is an anchor (no deltas). |
| `temporal.anchor_indices` | int[] | `[0, k, 2k, ...]` for `k = anchor_interval`, `< n_time`. |
| `temporal.delta_reference` | object | Key: non-anchor timestep index as a decimal string (JSON object keys). Value: the anchor index it references (§4). Every non-anchor index MUST appear. |
| `volatility_path` | string | Path of the volatility array relative to the root. `"volatility"` in v0.1. |

The temporal block applies to every level: anchor schedule and references are shared across the pyramid.

### 3.3 Level group attributes

| Key | Rule |
|---|---|
| `crs` | Same string as `chronozarr.crs`. |
| `transform` | 6 floats, rasterio/Affine order `[a, b, c, d, e, f]`: `x = a*col + b*row + c`, `y = d*col + e*row + f` for pixel corners. Level `k`: `[a*2^k, b, c, d, e*2^k, f]` (origin preserved). |
| `resolution` | Ground sample distance in CRS units at this level: `abs(a) * 2^k`. |

The data array SHOULD additionally carry `proj:code`, `spatial:dimensions` (`["y","x"]`), `spatial:shape`, `spatial:transform` and `spatial:bbox` (zarr-conventions `proj` and `spatial`, same values). zarr-layer reads these to detect a non-4326/3857 CRS instead of inferring it from bounds magnitude. Readers MUST NOT require them.

### 3.4 `multiscales` (ndpyramid schema)

One entry with `datasets[]`, `type` and `metadata` (instance in §3.5).

- `datasets[i].path` is the level group name; `pixels_per_tile` equals `cs`; `crs` equals `chronozarr.crs`.
- Levels MUST be listed in order `"0"`, `"1"`, ... with no gaps.
- `type` and `metadata.{method, version, args}` follow the ndpyramid schema page verbatim; `method` is nested under `metadata`, not top-level.
- The newer zarr-conventions `multiscales` (object with `layout[]`) uses the same key with a different shape; a store cannot carry both. v0.1 uses the ndpyramid list form because zarr-layer reads it. Revisit in v0.2.

### 3.5 Complete example

Store: 3 timesteps, 2 bands, 700 x 600 pixels, `cs = 512`, EPSG:32631 at 10 m. LOD 0 grid 2 x 2; LOD 1 is 350 x 300, grid 1 x 1, so the pyramid stops there.

`zarr.json` (root):

```json
{
  "zarr_format": 3,
  "node_type": "group",
  "attributes": {
    "multiscales": [{
      "datasets": [
        { "path": "0", "pixels_per_tile": 512, "crs": "EPSG:32631" },
        { "path": "1", "pixels_per_tile": 512, "crs": "EPSG:32631" }
      ],
      "type": "reduce",
      "metadata": { "method": "block_mean", "version": "chronozarr 0.1.0", "args": [] }
    }],
    "chronozarr": {
      "spec_version": "0.1.0",
      "variable": "data",
      "times": ["2024-01-01T00:00:00Z", "2024-02-01T00:00:00Z", "2024-03-01T00:00:00Z"],
      "bands": ["B04", "B08"],
      "nodata": 0,
      "crs": "EPSG:32631",
      "temporal": {
        "encoding": "star-delta",
        "anchor_interval": 2,
        "anchor_indices": [0, 2],
        "delta_reference": { "1": 0 }
      },
      "volatility_path": "volatility"
    }
  }
}
```

`0/zarr.json` (level group): `{ "zarr_format": 3, "node_type": "group", "attributes": { "crs": "EPSG:32631", "transform": [10.0, 0.0, 746090.0, 0.0, -10.0, 2540440.0], "resolution": 10.0 } }`

`0/data/zarr.json` (unsharded):

```json
{
  "zarr_format": 3,
  "node_type": "array",
  "shape": [3, 2, 700, 600],
  "data_type": "uint16",
  "chunk_grid": { "name": "regular", "configuration": { "chunk_shape": [1, 2, 512, 512] } },
  "chunk_key_encoding": { "name": "default", "configuration": { "separator": "/" } },
  "fill_value": 0,
  "codecs": [
    { "name": "bytes", "configuration": { "endian": "little" } },
    { "name": "zstd", "configuration": { "level": 5, "checksum": false } }
  ],
  "dimension_names": ["time", "band", "y", "x"],
  "attributes": { "_ARRAY_DIMENSIONS": ["time", "band", "y", "x"], "nodata": 0 }
}
```

Chunk objects: `0/data/c/{0,1,2}/0/{0,1}/{0,1}` (12 objects). Chunks at `r=1` hold rows 512..699 plus 324 rows of fill; chunks at `c=1` hold columns 512..599 plus 424 columns of fill.

`1/zarr.json` has `"transform": [20.0, 0.0, 746090.0, 0.0, -20.0, 2540440.0]`, `"resolution": 20.0`. `1/data/zarr.json` differs from level 0 only in `"shape": [3, 2, 350, 300]`. Chunk objects: `1/data/c/{0,1,2}/0/0/0`.

`0/time/zarr.json`:

```json
{
  "zarr_format": 3, "node_type": "array", "shape": [3], "data_type": "int64", "fill_value": 0,
  "chunk_grid": { "name": "regular", "configuration": { "chunk_shape": [3] } },
  "chunk_key_encoding": { "name": "default", "configuration": { "separator": "/" } },
  "codecs": [{ "name": "bytes", "configuration": { "endian": "little" } }, { "name": "zstd", "configuration": { "level": 5, "checksum": false } }],
  "dimension_names": ["time"],
  "attributes": { "_ARRAY_DIMENSIONS": ["time"], "units": "milliseconds since 1970-01-01T00:00:00", "calendar": "proleptic_gregorian" }
}
```

`0/time/c/0` decodes to `[1704067200000, 1706745600000, 1709251200000]`. `band`, `x` and `y` follow the same pattern with `data_type` `string` (codec `vlen-utf8`) or `int32`, `float64` and `float64` respectively. `volatility/zarr.json`: `shape [2, 2]`, `data_type "float32"`, `chunk_shape [2, 2]`, `fill_value 0.0`, `dimension_names ["row", "col"]`.

## 4. Star-delta temporal encoding

Semantics unchanged from ChronoFabric v1 §2. Applies identically at every level.

### Algorithm

Given `n_time` and `anchor_interval`:

1. **Anchor indices:** `[0, anchor_interval, 2*anchor_interval, ...]` while `< n_time`.
2. **Delta reference:** each non-anchor timestep references the nearest anchor by absolute index distance. Ties break toward the earlier anchor.
3. **Anchor storage:** `data[t] = source[t]` as uint16 (true values).
4. **Delta storage:** `data[t] = (source[t].int32 - anchor.int32).clip(-32768, 32767).int16.view(uint16)`.

Anchors and deltas live in the same uint16 array. A delta chunk is int16 residuals reinterpreted bit-for-bit as uint16. The clip is a guard: an encoder MUST verify roundtrip equality and fail if any residual was clipped, so Invariant 1 holds for every conforming store.

### Decoding

```
if t in anchor_indices:
    return data[t]                                              # uint16, use directly
else:
    anchor_t = chronozarr.temporal.delta_reference[str(t)]
    anchor   = data[anchor_t]                                   # uint16
    delta    = data[t].view(int16)                              # reinterpret
    return (anchor.int32 + delta.int32).clip(0, 65535).uint16
```

### Why star, not chain

Chain-delta (each timestep references the previous) gives roughly 10-15% better compression but requires sequential decoding. Star-delta gives O(1) random access to any timestep, which is what scrubbing needs.

## 5. Volatility

A float32 array at `volatility_path` (`volatility`) at the root group, shape `(grid_rows_0, grid_cols_0)`, one value per LOD 0 cell, single chunk.

```
volatility[r, c] = clip( mean(|source[t] - source[delta_reference[t]]|
                              over all non-anchor t, all bands, all pixels of cell (r, c))
                         / 10000 , 0, 1)
```

- Computed in int32, over all pixels including nodata, at LOD 0 only.
- `0.0` when the cell has no delta timesteps (`anchor_interval = 1` or `n_time = 1`).
- The divisor 10000 is a fixed normalization constant in v0.1 (Sentinel-2 reflectance scale). It is not a physical unit; for other uint16 sources the value is still monotone in temporal change.

Readers use it to order prefetch (volatile cells first) and to draw change overviews. A store without it is decodable but not conforming.

## 6. Multiscale pyramid

Semantics unchanged from ChronoFabric v1 §3.

Each level halves pixel dimensions and doubles ground sample distance by area-weighted block averaging that excludes `nodata` pixels. A block whose pixels are all `nodata` yields `nodata`.

- Level 0: native resolution (10 m for Sentinel-2). Level `k`: resolution `* 2^k`.
- Level `k` is derived from level `k-1` by a factor-2 block mean. Before averaging, the source is padded to an even height and width by edge replication, so `H_k = ceil(H_{k-1} / 2) = ceil(H_0 / 2^k)`, same for `W`.
- The mean is computed in integer arithmetic (uint32 sum, integer division by the valid-pixel count) and stored as uint16.
- Downsampling is applied to source timesteps; star-delta is then applied per level. Deltas are never downsampled.
- **Chunk size stays constant.** Only the grid shrinks: `grid_rows_k = ceil(H_k / cs)`.
- Levels MUST be consecutive from 0. The encoder default stops at the first level whose grid is 1 x 1 (that level is included). Fewer or more levels MAY be present.

## 7. Sharding

**Default: sharded.** Decided by the P0 spike (2026-09-29): zarrita 0.7.5 read sharded stores over HTTP byte ranges with bytes transferred equal to the unsharded store and bit-exact reconstruction on all three fixture variants. The default is the `sharding_indexed` codec with shard shape `(n_time, n_band, cs, cs)` and inner chunk shape `(1, n_band, cs, cs)`. One shard object then holds a spatial cell's whole time axis, and one timestep is one HTTP byte-range read.

```json
"chunk_grid": { "name": "regular", "configuration": { "chunk_shape": [3, 2, 512, 512] } },
"codecs": [{
  "name": "sharding_indexed",
  "configuration": {
    "chunk_shape": [1, 2, 512, 512],
    "codecs": [{ "name": "bytes", "configuration": { "endian": "little" } }, { "name": "zstd", "configuration": { "level": 5, "checksum": false } }],
    "index_codecs": [{ "name": "bytes", "configuration": { "endian": "little" } }, { "name": "crc32c" }],
    "index_location": "end"
  }
}]
```

- In Zarr v3 terms the array's `chunk_grid.chunk_shape` is the **shard** shape and the codec's `chunk_shape` is the **inner chunk** shape. Inner chunks are the same bytes an unsharded store would hold as separate objects.
- Shard keys: `c/0/0/{r}/{c}` (the time shard index is always 0). Objects per level drop from `n_time * grid_rows * grid_cols` to `grid_rows * grid_cols`.
- The shard index holds `n_time` `(offset, nbytes)` uint64 pairs: `16 * n_time` bytes plus 4 bytes crc32c. With `index_location: "end"` a reader fetches it with a suffix range (`Range: bytes=-N`); with `"start"`, a prefix range. Readers MUST support both. Writers SHOULD use `"end"` in v0.1: zarrita 0.7.5 ignores `index_location` and decodes start-indexed shards as garbage without error, which would break every stock zarrita reader including CarbonPlan zarr-layer. Readers MUST cache the index per cell and reuse one array handle per level so a timestep costs exactly one further range read (zarrita caches the index per array instance; it issues one `HEAD` before the index read, so a cold cell costs `HEAD` + index + chunk, then one range per timestep).
- Empty inner chunks (both index values `2^64 - 1`) decode as all `fill_value`.

**Both forms are valid v0.1 stores.** Unsharded (§2.1) remains valid for hosts without byte-range support. A reader MUST handle both; it learns which from the `codecs` list.

## 8. Compression codec

| Codec | Status | Configuration |
|---|---|---|
| `zstd` | Default | `{ "level": 5, "checksum": false }` |
| `gzip` | Permitted | `{ "level": 1..9 }` |

- Readers MUST support both. Writers MUST use exactly one, preceded by `bytes` little-endian.
- `blosc` and other codecs MUST NOT be used in v0.1: the browser reader carries no WASM dependency for them.
- Rationale: browser decode speed decides, and it was measured, not assumed. P0 spike (2026-09-29, one real Sentinel-2 chunk of 4 x 512 x 512 uint16 = 2,097,152 bytes, median of 15 in Chromium): zstd via the numcodecs-js WASM codec that zarrita uses, 4.5 ms at 1,284,781 bytes; gzip via native `DecompressionStream`, 6.7 ms at 1,387,409 bytes; zstd via the pure-JS fzstd, 14.1 ms. zstd is both faster and 8% smaller, so it is the default. gzip stays permitted for writers without a zstd implementation. Encode cost is irrelevant.

## 9. Static hosting requirements

A chronozarr store is served by any HTTP server or object store that returns files by path. No server-side code.

| Requirement | Level |
|---|---|
| `GET {store}/{key}` returns the object bytes | MUST |
| `Range` requests honoured with `206 Partial Content` and `Content-Range` (needed for sharded stores; unsharded stores need only GET) | MUST for sharded |
| `Access-Control-Allow-Origin: *` | MUST |
| `Access-Control-Allow-Headers: Range` (or `*`) and a `200`/`204` answer to `OPTIONS` preflight | SHOULD (browsers do not preflight a bounded `Range: bytes=a-b`; only suffix ranges `bytes=-N` trigger one) |
| `Access-Control-Expose-Headers: Content-Range, Content-Length` | SHOULD |
| `Cache-Control: public, max-age=31536000, immutable` | SHOULD |

- Stores are treated as immutable. A re-encode MUST be written under a new prefix, never in place.
- Zarr v3 metadata is `zarr.json`, never a dotfile, so hosts that hide dotfiles (GitHub Pages, some CDNs) serve a store correctly. No `.zarray`, `.zattrs` or `.zmetadata` exist.
- Directory listing is never required (a reader derives every key from metadata) and object content types are ignored.

## 10. Reader requirements

A conforming reader MUST:

1. `GET {store}/zarr.json`. Reject the store if `attributes.chronozarr.spec_version` is not `0.1.x` or `temporal.encoding` is not `"star-delta"`. Take timestamps from `chronozarr.times`, band names from `chronozarr.bands`, the data array name from `chronozarr.variable`.
2. Enumerate levels from `multiscales[0].datasets[].path`. Read each level's `zarr.json` (`transform`, `resolution`) and `{variable}/zarr.json` (`shape`, `chunk_grid`, `codecs`), or take them from consolidated metadata when present.
3. Select a level: the largest `k` whose `resolution` does not exceed the requested output ground sample distance; `k = 0` if none.
4. For timestep `t` and cell `(r, c)`: if `t` is in `anchor_indices`, read one chunk. Otherwise read the chunk at `delta_reference[str(t)]` and the chunk at `t`. Never more than two reads (Invariant 2).
5. Reconstruct per §4: int32 add, clip to `[0, 65535]`, uint16. Discard elements beyond `shape` (§2.1).
6. For sharded arrays, read the shard index, then one byte range per inner chunk (§7).
7. Treat `nodata` as missing in band math and statistics.

A reader SHOULD cache decoded anchors and shard indices for the session, and SHOULD prefetch anchors before deltas: once every anchor in the viewport is resident, any timestep costs one delta read.

Reference implementations: Python `chronozarr.open(store) -> xarray.DataArray`, dims `(time, band, y, x)`, lazily reconstructing per timestep, plus `chronozarr.validate(store)`; JavaScript `chronozarr/decoder.js` (zarrita store, star-delta reconstruct, cache, prefetch), DOM-free so it runs in a Worker, under MapLibre, or on a bare canvas.

A plain Zarr reader (`xarray.open_zarr(store, group="0")`, zarrita) reads the same store without this spec: anchor timesteps are correct, non-anchor timesteps are residuals.

## 11. What this is not

- Not a new binary format. It is Zarr v3 with a layout convention.
- Not a codec. Star-delta spans chunks, so it cannot sit in a Zarr `codecs` list; it lives in group attributes and the reader.
- Not a video codec. Deltas are block-compressed arrays, not I/P/B frames.
- Not a spatial index. A regular grid of chunks with a power-of-2 pyramid.
- Not a server or an API. There is nothing to run; a bucket is the deployment.
- Not a viewer. TileRipper is one consumer; zarr-layer and xarray are others.

The novelty is star-delta temporal encoding inside a standard Zarr v3 pyramid, decoded in the browser from raw uint16 bands.

## 12. Relationship to prior art

- **PMTiles** (Protomaps): single-file archive of z/x/y visual tiles for static hosting. The hosting model (one bucket, range reads, no server) is the same. It carries no time axis and no queryable values; tiles are rendered pixels.
- **Mapbox raster-array / MRT**: multi-band numeric raster tiles with a time-like band dimension, decoded client-side. The decoder and format are proprietary to Mapbox GL; MapLibre has no equivalent. chronozarr is the open counterpart.
- **carbonplan ndpyramid + zarr-layer**: Zarr pyramids rendered in MapLibre with a time selector. chronozarr writes the same `multiscales` attribute and level/variable shape so zarr-layer can read it as-is. Differences: zarr-layer has no cross-time compression (each timestep is a full chunk), and its tiled path expects EPSG:4326 or EPSG:3857, while chronozarr stores stay in a projected per-AOI CRS and declare it via `proj`/`spatial` attributes.
- **GeoZarr** and the zarr-conventions `proj`/`spatial`/`multiscales` drafts: CRS and affine conventions chronozarr aligns with (`crs`, `transform`, `proj:code`, `spatial:*`). chronozarr adds only the temporal block, the `times`/`bands` mirrors and the volatility array on top.
