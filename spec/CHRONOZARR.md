# chronozarr v0.2

**Status:** Draft
**Date:** 2026-09-30
**Spec version string:** `0.2.0`
**Supersedes:** chronozarr v0.1 (`0.1.0`); `CHRONOFABRIC.md` (ChronoFabric v1: Zarr v2, one array per cell, `manifest.json`)

## 0. Purpose

chronozarr is a Zarr v3 layout convention for raster time series (satellite image stacks, gridded products). A browser opens a decade of analysis-ready data from a static bucket, scrubs through time like video, and returns the exact stored value on click. It is a **convention plus a reader**. It is not a codec and not a new container: every object in a chronozarr store is a plain Zarr v3 object, readable by any Zarr v3 library that supports the store's codecs.

A store uses one of two temporal encodings (§4). With `none`, the data array holds the true values of every timestep and any Zarr v3 reader reads the store without this spec. With `star-delta`, non-anchor timesteps hold residuals against an anchor timestep, and a chronozarr-aware reader is required to reconstruct them. The writer chooses between the two per store and enables `star-delta` only where it reduces compressed size (§4.3).

The star-delta encoding cannot be a per-chunk Zarr codec: decoding a delta timestep needs the anchor chunk, which is a different chunk. A plain Zarr reader (`xarray.open_zarr`, zarrita) of a `star-delta` store sees anchor timesteps correctly and non-anchor timesteps as residuals in the stored dtype. This is the documented cost of that encoding.

MUST, MUST NOT, SHOULD, SHOULD NOT, MAY are used as in RFC 2119.

## 1. Invariants

1. **Lossless roundtrip.** `encode(data)` then a read of level 0 returns the exact input values in the stored dtype, under either temporal encoding. No quantization, no rounding, no lossy compression of the data; coarser levels are block means of it (§6).
2. **O(1) random timestep access.** Any timestep at any level decodes with at most two chunk reads: one anchor plus one delta (one read under `none`). No chain accumulation, no sequential dependency. The two chunks may live in different shards (§7.4).
3. **dtype preservation.** Values are stored and served in the store's dtype (§2.3). The format never converts between integer and float and never quantizes. Physical values are derived at read time from `scale` and `offset` (§3.6); they are not stored.
4. **Self-describing.** Group and array attributes fully describe layout, temporal encoding, CRS and chunk grid. No sidecar files, no external metadata.

## 2. Store layout

A chronozarr store is a Zarr v3 hierarchy. Zarr v3 arrays are leaf nodes, so each LOD level is a **group** named `"0"`, `"1"`, ... holding one data array (default name `data`, declared in `chronozarr.variable`), optional `mask` and `coverage` arrays, and coordinate arrays. This is the shape ndpyramid writes and carbonplan/zarr-layer reads (`{store}/{level}/{variable}`).

```
{store}/
  zarr.json                      root group; attrs: multiscales, chronozarr                (§3.1)
  volatility/
    zarr.json                    array (grid_rows_0, grid_cols_0) float32                  (§5)
    c/0/0
  0/
    zarr.json                    level group; attrs: crs, transform, resolution            (§3.3)
    data/
      zarr.json                  array (time, band, y, x), chunks (1, n_band, cs, cs)      (§2.1)
      c/{ts}/0/{r}/{c}           sharded (default): one object per (time shard, cell)      (§7)
      c/{t}/0/{r}/{c}            unsharded: one object per (timestep, cell)
    mask/                        optional: array (time, y, x) uint8                        (§2.4)
      zarr.json
      c/{ts}/{r}/{c}             sharded; unsharded is c/{t}/{r}/{c}
    coverage/                    optional: array (time, y, x) uint8                        (§2.5)
      zarr.json
      c/{ts}/{r}/{c}
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
| `data_type` | `uint8`, `uint16`, `int16` or `float32` (§2.3). Identical at every level. |
| `chunk_grid` | `regular`. Unsharded: `chunk_shape = [1, n_band, cs, cs]`. Sharded: §7. |
| `chunk_key_encoding` | `default`, separator `/`, so keys are `c/...` |
| `fill_value` | `chronozarr.nodata` when it is a number; otherwise `0` |
| `codecs` | `bytes` (`endian: little`) then one compression codec (§8), inside `sharding_indexed` when sharded |
| `dimension_names` | `["time", "band", "y", "x"]` |
| `attributes` | `_ARRAY_DIMENSIONS: ["time","band","y","x"]`; `nodata` (same value as `chronozarr.nodata`) present only when that value is a number |

- `cs` (chunk size) MUST be identical at every level and even. Writers SHOULD use 256 or 512 (default 512; 256 suits low-latency stores). Readers MUST take `cs` from `multiscales[0].datasets[].pixels_per_tile`, MUST NOT assume either value, and MUST NOT reject another even value (the reference writer accepts smaller even sizes for test fixtures).
- The band chunk index is always 0: one chunk holds every band of one timestep of one cell.
- Cell `(r, c)` at level `k` is array slice `[:, :, r*cs:(r+1)*cs, c*cs:(c+1)*cs]`; row 0 is the top (north) edge. `grid_rows_k = ceil(H_k / cs)`, `grid_cols_k = ceil(W_k / cs)`.
- **Edge chunks** are ordinary Zarr chunks: stored at full `cs x cs`; elements outside `shape` equal `fill_value`. Readers MUST discard elements beyond `shape`. There is no edge-size formula and no per-chunk size metadata. Padding is `fill_value` in anchors and 0 in residuals, so it costs almost nothing after compression.

### 2.2 Coordinate arrays `{level}/{time,band,y,x}`

| Array | `data_type` | Contents |
|---|---|---|
| `time` | `int64` | Milliseconds since Unix epoch. Attrs `units: "milliseconds since 1970-01-01T00:00:00"`, `calendar: "proleptic_gregorian"` (CF), so xarray decodes to `datetime64` natively. MUST be strictly increasing. Any cadence: monthly, weekly, irregular. |
| `band` | `string` (codec `vlen-utf8`) or `int32` | Band names in array order where the writer supports Zarr v3 strings; otherwise the band index `0..n_band-1`. |
| `y` | `float64` | Projected y of pixel centres in `crs`, decreasing (north up). |
| `x` | `float64` | Projected x of pixel centres in `crs`, increasing. |

- Every array in the store MUST set `dimension_names` and the attribute `_ARRAY_DIMENSIONS` to the same list (`dimension_names` is native Zarr v3; `_ARRAY_DIMENSIONS` is what xarray-v2-era tools read).
- Each coordinate array SHOULD be a single chunk. `time` and `band` MUST be identical at every level.
- The timestamps as ISO-8601 strings and the band names are duplicated in root attrs `chronozarr.times` and `chronozarr.band_names` (§3.2), so a JS reader never needs int64 or vlen strings. `chronozarr.times[i]` MUST be the ISO-8601 rendering of `time[i]`; `chronozarr.band_names[i]` MUST equal `band[i]` where `band` holds names.

### 2.3 dtype and nodata profiles

| `data_type` | `nodata` | `star-delta` | Pyramid mean (§6) |
|---|---|---|---|
| `uint8` | integer 0 to 255, or `null` | allowed | integer |
| `uint16` | integer 0 to 65535, or `null` | allowed | integer |
| `int16` | integer, or `null` | not allowed (`none`) | integer |
| `float32` | finite number, or `null` | not allowed (`none`) | float |

- `chronozarr.nodata` is one number or `null`. `null` means the store declares no nodata value. The writer default is `0` for `uint8` and `uint16` (as in v0.1 for `uint16`) and `null` for `int16` and `float32`.
- When `nodata` is a number it MUST equal every data array's `fill_value` and `nodata` attribute. When it is `null`, `fill_value` is `0` and the attribute is absent.
- **Validity.** A pixel is *valid* if `mask` exists and is 1 at that pixel; or, when no mask exists and `nodata` is a number, if its value differs from `nodata`; or, when neither applies, always. A reader that has a mask MUST use it and MUST NOT also compare against `nodata`. Invalid pixels are missing in band math, statistics, block means and charts.
- Where `mask` is 0, the stored data value is not interpreted; writers SHOULD store `fill_value` there.
- NaN is not interpreted by this spec and is not a valid `nodata`. Writers SHOULD NOT store NaN; they SHOULD mark gaps with `nodata` or `mask`.
- `bytes` for a one-byte dtype has no byte order; its `endian` configuration MAY be omitted.

### 2.4 Mask `{level}/mask` (optional)

`uint8`, dims `["time", "y", "x"]`, shape `[n_time, H_k, W_k]`, values 1 (valid) and 0 (invalid), `fill_value` 0, `_ARRAY_DIMENSIONS` set. Chunked like `data` without the band axis: unsharded `[1, cs, cs]`; sharded per §7 with shard shape `[shard_time, cs, cs]` and inner chunk `[1, cs, cs]`. Keys carry no band index: `c/{ts}/{r}/{c}`.

- A store has a mask at every level or at none. When present, root attr `chronozarr.mask_variable` is `"mask"`; when absent, that attribute is absent.
- The mask is always stored as true values: it is never star-delta encoded.
- Level `k` mask is the maximum over each 2 x 2 block of level `k-1` (1 if any source pixel is valid).

### 2.5 Coverage `{level}/coverage` (optional)

`uint8`, dims `["time", "y", "x"]`, shape `[n_time, H_k, W_k]`. The value is the number of valid observations behind the pixel (saturating at 255); `0` means the pixel was gap-filled or never observed. `fill_value` 0. Layout and chunking are identical to `mask` (§2.4), and it is never star-delta encoded.

- A store has coverage at every level or at none. When present, `chronozarr.coverage_variable` is `"coverage"`; when absent, that attribute is absent.
- Level `k` coverage is the mean of each 2 x 2 block of level `k-1`, rounded to the nearest integer with halves rounded up: `(sum + 2) // 4`.
- Coverage is independent of `mask`: a gap-filled pixel is valid data with coverage 0. How gaps were filled is recorded in `chronozarr.provenance.gap_fill` (§3.8).

## 3. Attributes

### 3.1 Root group

| Key | Type | Rule |
|---|---|---|
| `multiscales` | list | ndpyramid schema (§3.4). Exactly one entry. |
| `chronozarr` | object | §3.2. |

Writers SHOULD also write zarr-python 3 `consolidated_metadata` into the root `zarr.json` so a reader learns every array's shape and codecs in one GET. Readers MUST fall back to per-array `zarr.json` when it is absent.

### 3.2 `chronozarr` block

Complete instance in the root example, §3.9.

| Field | Type | Rule |
|---|---|---|
| `spec_version` | string | `0.2.x` for stores written to this spec. Readers MUST accept `0.1.x` and `0.2.x` and MUST reject every other version. |
| `variable` | string | Name of the data array inside each level group. Default `"data"`. Readers MUST NOT hardcode it. |
| `times` | string[] | ISO-8601 timestamp per timestep, length `n_time`, equal to the `time` coordinate. |
| `bands` | object[] | Band objects (§3.6) in array order, length `n_band`. Readers MUST also accept the v0.1 form, a list of strings, each read as a band object with only `name`. |
| `band_names` | string[] | `name` of each entry of `bands`, in order. Writers MUST write it. Readers that find only `bands` derive it. |
| `nodata` | number or null | §2.3. |
| `crs` | string | `EPSG:<code>`. MUST equal every level's `crs` and every `multiscales.datasets[].crs`. A projected per-AOI CRS (UTM) SHOULD be used. Web Mercator SHOULD NOT: its pixels are not equal-area, so block means and statistics are biased. |
| `temporal` | object | §4.1. |
| `volatility_path` | string | Path of the volatility array relative to the root. `"volatility"` in v0.2. |
| `mask_variable` | string | Optional. `"mask"` when `{level}/mask` exists (§2.4). |
| `coverage_variable` | string | Optional. `"coverage"` when `{level}/coverage` exists (§2.5). |
| `provenance` | object | Optional. §3.8. |
| `levels` | object[] | §3.7. Writers MUST write it; readers MUST fall back to level metadata when it is absent. |
| `shard_bytes` | object | Optional, sharded stores only. §7.3. |

The temporal block applies to every level: anchor schedule and references are shared across the pyramid.

### 3.3 Level group attributes

| Key | Rule |
|---|---|
| `crs` | Same string as `chronozarr.crs`. |
| `transform` | 6 floats, rasterio/Affine order `[a, b, c, d, e, f]`: `x = a*col + b*row + c`, `y = d*col + e*row + f` for pixel corners. Level `k`: `[a*2^k, b, c, d, e*2^k, f]` (origin preserved). |
| `resolution` | Ground sample distance in CRS units at this level: `abs(a) * 2^k`. |

The data array SHOULD additionally carry `proj:code`, `spatial:dimensions` (`["y","x"]`), `spatial:shape`, `spatial:transform` and `spatial:bbox` (zarr-conventions `proj` and `spatial`, same values). zarr-layer reads these to detect a non-4326/3857 CRS instead of inferring it from bounds magnitude. When `crs` is an EPSG code the writer SHOULD also set `_CRS` on the array, `{ "url": "http://www.opengis.net/def/crs/EPSG/0/<code>" }` optionally with a `wkt` member: GDAL's Zarr driver reads this attribute to assign a CRS (its documentation names it the only CRS convention supported before GDAL 3.13). The array MAY also repeat `crs` and `transform`. Readers MUST NOT require any of these.

### 3.4 `multiscales` (ndpyramid schema)

One entry with `datasets[]`, `type` and `metadata` (instance in §3.9).

- `datasets[i].path` is the level group name; `pixels_per_tile` equals `cs`; `crs` equals `chronozarr.crs`.
- Levels MUST be listed in order `"0"`, `"1"`, ... with no gaps.
- `type` and `metadata.{method, version, args}` follow the ndpyramid schema page verbatim; `method` is nested under `metadata`, not top-level. `metadata.version` is `chronozarr 0.2.0` for new stores.
- The zarr-conventions `multiscales` (object with `layout[]`) uses the same key with a different shape; a store cannot carry both. v0.2 keeps the ndpyramid list form because zarr-layer reads it and the GeoZarr layout is still a proposal. Revisit when GeoZarr's multiscale layout is stable.

### 3.5 Mirrors

Three root attributes duplicate information that also lives in arrays or level groups, so a reader needs no vlen strings, int64 or per-level requests: `chronozarr.times` (mirrors the `time` coordinate), `chronozarr.band_names` (mirrors the `band` coordinate and `chronozarr.bands`) and `chronozarr.levels` (mirrors level attributes and array shapes, §3.7). A mirror MUST agree with its source; a validator checks this. Readers use the mirror as the primary source and MUST NOT require the source array to be read.

### 3.6 Band objects

Each entry of `chronozarr.bands` is an object:

| Field | Type | Rule |
|---|---|---|
| `name` | string | Required. Unique within the store. Equals the `band` coordinate value where that holds names. |
| `common_name` | string | Optional. SHOULD come from the STAC `eo:bands` common-name vocabulary where one applies (`red`, `green`, `blue`, `nir`, `swir16`, `swir22`, ...). |
| `scale` | number | Optional, default 1. |
| `offset` | number | Optional, default 0. |
| `units` | string | Optional. Free text, for example `"reflectance"`. |

- Physical value = `stored * scale + offset`. Invalid pixels (§2.3) are not scaled; they are missing.
- The v0.1 string form carries no `scale`, so it reads as `scale` 1. A consumer that knows the source may supply one: the JavaScript reader assumes 0.0001, the Sentinel-2 L2A reflectance scale, for `0.1.x` stores. New stores SHOULD write `scale` and `offset` explicitly.
- chronozarr does not set CF `scale_factor` or `add_offset` on the data array, so plain Zarr readers see stored values and scale-aware decoding is the consumer's choice.
- Consumers that do band math or compute indices SHOULD select bands by `common_name`, falling back to `name`, and MUST apply `scale` and `offset` rather than assume a fixed reflectance scale. An RGB `uint8` store with bands `red`, `green`, `blue` and `scale` 1 therefore renders as stored.

### 3.7 `levels` mirror

`chronozarr.levels` is a list with one object per level, in level order, mirroring level metadata so a reader opens the store with one GET:

| Field | Type | Rule |
|---|---|---|
| `path` | string | Level group name; equals `multiscales[0].datasets[i].path`. |
| `resolution` | number | Equals the level group's `resolution`. |
| `transform` | number[6] | Equals the level group's `transform`. |
| `shape` | int[4] | `[n_time, n_band, H_k, W_k]`; equals the data array's `shape`. |
| `grid` | int[2] | `[rows, cols]` of cells; equals `[ceil(H_k / cs), ceil(W_k / cs)]`. |

The mirror MUST agree with the level group and array metadata; a validator checks it.

### 3.8 `provenance` (optional)

```json
{ "sources": ["sentinel-2-l2a"], "composite": "monthly median", "gap_fill": "carry-forward", "notes": "SCL classes 4, 5, 6, 7 and 11 kept" }
```

| Field | Type | Rule |
|---|---|---|
| `sources` | string[] | Required. Collection ids or URLs of the input data. |
| `composite` | string | Required. Free text describing the temporal composite, for example `"monthly median"`. |
| `gap_fill` | string | Required. `"carry-forward"` (a pixel with no valid observation takes the previous timestep's value) or `"none"`. |
| `notes` | string | Optional. |

### 3.9 Complete example

Store: 5 monthly timesteps, 2 bands, 700 x 600 pixels, `cs = 512`, EPSG:32631 at 10 m, `star-delta` with `anchor_interval = 2`, `shard_time = 4` (two time shards), with `mask` and `coverage`. LOD 0 grid is 2 x 2; LOD 1 is 350 x 300, grid 1 x 1, so the pyramid stops there. Shard byte lengths are illustrative. The example uses `zstd` level 5, the writer default (§8).

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
      "metadata": { "method": "block_mean", "version": "chronozarr 0.2.0", "args": [] }
    }],
    "chronozarr": {
      "spec_version": "0.2.0",
      "variable": "data",
      "times": ["2024-01-01T00:00:00Z", "2024-02-01T00:00:00Z", "2024-03-01T00:00:00Z", "2024-04-01T00:00:00Z", "2024-05-01T00:00:00Z"],
      "bands": [
        { "name": "B04", "common_name": "red", "scale": 0.0001, "offset": 0.0, "units": "reflectance" },
        { "name": "B08", "common_name": "nir", "scale": 0.0001, "offset": 0.0, "units": "reflectance" }
      ],
      "band_names": ["B04", "B08"],
      "nodata": 0,
      "crs": "EPSG:32631",
      "temporal": {
        "encoding": "star-delta",
        "anchor_interval": 2,
        "anchor_indices": [0, 2, 4],
        "delta_reference": { "1": 0, "3": 2 },
        "selection": { "mode": "auto", "sampled_cells": 3, "ratio": 0.78 }
      },
      "volatility_path": "volatility",
      "mask_variable": "mask",
      "coverage_variable": "coverage",
      "provenance": {
        "sources": ["sentinel-2-l2a"],
        "composite": "monthly median",
        "gap_fill": "carry-forward",
        "notes": "SCL classes 4, 5, 6, 7 and 11 kept"
      },
      "levels": [
        { "path": "0", "resolution": 10.0, "transform": [10.0, 0.0, 746090.0, 0.0, -10.0, 2540440.0], "shape": [5, 2, 700, 600], "grid": [2, 2] },
        { "path": "1", "resolution": 20.0, "transform": [20.0, 0.0, 746090.0, 0.0, -20.0, 2540440.0], "shape": [5, 2, 350, 300], "grid": [1, 1] }
      ],
      "shard_bytes": {
        "0": {
          "0/0/0": 1412807, "0/0/1": 388120, "0/1/0": 1290455, "0/1/1": 351902,
          "1/0/0": 402211, "1/0/1": 101347, "1/1/0": 377689, "1/1/1": 96410
        },
        "1": { "0/0/0": 388554, "1/0/0": 98203 }
      }
    }
  }
}
```

`0/zarr.json` (level group): `{ "zarr_format": 3, "node_type": "group", "attributes": { "crs": "EPSG:32631", "transform": [10.0, 0.0, 746090.0, 0.0, -10.0, 2540440.0], "resolution": 10.0 } }`

`0/data/zarr.json` (sharded, two time shards of up to 4 timesteps):

```json
{
  "zarr_format": 3,
  "node_type": "array",
  "shape": [5, 2, 700, 600],
  "data_type": "uint16",
  "chunk_grid": { "name": "regular", "configuration": { "chunk_shape": [4, 2, 512, 512] } },
  "chunk_key_encoding": { "name": "default", "configuration": { "separator": "/" } },
  "fill_value": 0,
  "codecs": [{
    "name": "sharding_indexed",
    "configuration": {
      "chunk_shape": [1, 2, 512, 512],
      "codecs": [{ "name": "bytes", "configuration": { "endian": "little" } }, { "name": "zstd", "configuration": { "level": 5, "checksum": false } }],
      "index_codecs": [{ "name": "bytes", "configuration": { "endian": "little" } }, { "name": "crc32c" }],
      "index_location": "end"
    }
  }],
  "dimension_names": ["time", "band", "y", "x"],
  "attributes": { "_ARRAY_DIMENSIONS": ["time", "band", "y", "x"], "nodata": 0 }
}
```

Shard objects: `0/data/c/{0,1}/0/{0,1}/{0,1}` (8 objects). Timesteps 0 to 3 are in time shard 0 (inner position `t`), timestep 4 is in time shard 1 at inner position 0. Shard `c/1/0/r/c` holds one timestep, but its index still has `shard_time = 4` entries (16 x 4 + 4 = 68 bytes), three of them empty. Shards at `r=1` hold rows 512..699 plus 324 rows of fill; shards at `c=1` hold columns 512..599 plus 424 columns of fill.

`0/mask/zarr.json` (`coverage` differs only by name and `_ARRAY_DIMENSIONS` is identical):

```json
{
  "zarr_format": 3,
  "node_type": "array",
  "shape": [5, 700, 600],
  "data_type": "uint8",
  "chunk_grid": { "name": "regular", "configuration": { "chunk_shape": [4, 512, 512] } },
  "chunk_key_encoding": { "name": "default", "configuration": { "separator": "/" } },
  "fill_value": 0,
  "codecs": [{
    "name": "sharding_indexed",
    "configuration": {
      "chunk_shape": [1, 512, 512],
      "codecs": [{ "name": "bytes" }, { "name": "zstd", "configuration": { "level": 5, "checksum": false } }],
      "index_codecs": [{ "name": "bytes", "configuration": { "endian": "little" } }, { "name": "crc32c" }],
      "index_location": "end"
    }
  }],
  "dimension_names": ["time", "y", "x"],
  "attributes": { "_ARRAY_DIMENSIONS": ["time", "y", "x"] }
}
```

Shard objects: `0/mask/c/{0,1}/{0,1}/{0,1}` (8 objects), `0/coverage/c/{0,1}/{0,1}/{0,1}` (8 objects).

`1/zarr.json` has `"transform": [20.0, 0.0, 746090.0, 0.0, -20.0, 2540440.0]`, `"resolution": 20.0`. Level 1 arrays differ from level 0 only in `shape` (`[5, 2, 350, 300]`, `[5, 350, 300]`). Shard objects: `1/data/c/{0,1}/0/0/0`, `1/mask/c/{0,1}/0/0`, `1/coverage/c/{0,1}/0/0`.

`0/time/zarr.json`:

```json
{
  "zarr_format": 3, "node_type": "array", "shape": [5], "data_type": "int64", "fill_value": 0,
  "chunk_grid": { "name": "regular", "configuration": { "chunk_shape": [5] } },
  "chunk_key_encoding": { "name": "default", "configuration": { "separator": "/" } },
  "codecs": [{ "name": "bytes", "configuration": { "endian": "little" } }, { "name": "zstd", "configuration": { "level": 5, "checksum": false } }],
  "dimension_names": ["time"],
  "attributes": { "_ARRAY_DIMENSIONS": ["time"], "units": "milliseconds since 1970-01-01T00:00:00", "calendar": "proleptic_gregorian" }
}
```

`0/time/c/0` decodes to `[1704067200000, 1706745600000, 1709251200000, 1711929600000, 1714521600000]`. `band`, `x` and `y` follow the same pattern with `data_type` `string` (codec `vlen-utf8`) or `int32`, `float64` and `float64` respectively. `volatility/zarr.json`: `shape [2, 2]`, `data_type "float32"`, `chunk_shape [2, 2]`, `fill_value 0.0`, `dimension_names ["row", "col"]`.

A `none` store differs in the `temporal` block only (`{ "encoding": "none" }`, optionally with `selection`); the arrays, layout and every other attribute are the same.

## 4. Temporal encoding

### 4.1 `temporal` block

| Field | Type | Rule |
|---|---|---|
| `encoding` | string | `"none"` or `"star-delta"`. Readers MUST support both. |
| `anchor_interval` | int >= 1 | `star-delta` only. Timesteps between anchors. `1` means every timestep is an anchor (no deltas). |
| `anchor_indices` | int[] | `star-delta` only. `[0, k, 2k, ...]` for `k = anchor_interval`, `< n_time`. |
| `delta_reference` | object | `star-delta` only. Key: non-anchor timestep index as a decimal string (JSON object keys). Value: the anchor index it references (§4.2). Every non-anchor index MUST appear. |
| `selection` | object | Optional. `{ "mode": "auto", "sampled_cells": int, "ratio": number }`, written only when the writer chose the encoding by measurement (§4.3). |

With `"none"`, the data array holds true values and readers do nothing beyond §10. `anchor_interval`, `anchor_indices` and `delta_reference` MUST be absent. `star-delta` is valid only for `uint8` and `uint16` data (§2.3).

### 4.2 Star-delta

Applies identically at every level.

1. **Anchor indices:** `[0, anchor_interval, 2*anchor_interval, ...]` while `< n_time`.
2. **Delta reference:** each non-anchor timestep references the nearest anchor by absolute index distance. Ties break toward the earlier anchor.
3. **Anchor storage:** `data[t] = source[t]` (true values).
4. **Delta storage:** `data[t] = (source[t] - source[a]) mod 2^bits`, where `a` is the reference anchor and `bits` is 8 or 16: unsigned wraparound subtraction in the stored dtype.

Anchors and deltas live in the same array. There is no clipping, no range check and no failure mode: modular arithmetic reconstructs every input exactly, so Invariant 1 holds for every input. When `|source[t] - source[a]| < 2^(bits-1)` the stored bits equal the two's-complement signed difference, which is what v0.1 stored for `uint16` (int16 residuals viewed as uint16); every v0.1 star-delta store is therefore also a valid v0.2 store.

Decoding:

```
if encoding == "none" or t in anchor_indices:
    return data[t]                                   # true values, use directly
else:
    anchor_t = delta_reference[str(t)]
    return (data[anchor_t] + data[t]) mod 2^bits     # unsigned wraparound in the stored dtype
```

Readers MUST NOT clamp. A residual is not special-cased for `nodata`: the stored residual of a nodata pixel is `(nodata - anchor) mod 2^bits`, and reconstruction returns `nodata` exactly.

**Why star, not chain.** Chain-delta (each timestep references the previous) gives roughly 10-15% better compression but requires sequential decoding. Star-delta gives O(1) random access to any timestep, which is what scrubbing needs.

### 4.3 Writer selection

The writer option `encoding` takes `auto` (default), `none` or `star-delta`.

- `auto` applies to `uint8` and `uint16` only. For `int16` and `float32` the writer uses `none` and writes no `selection`.
- `auto` encodes a sample of LOD 0 cells both ways with the codec the store will use, at least 3 cells or all cells when there are fewer, and keeps `star-delta` only if its total compressed bytes are at most 0.85 times the plain total. It records `selection = { "mode": "auto", "sampled_cells": n, "ratio": r }`, where `r` is star-delta compressed bytes divided by plain compressed bytes over the sampled cells.
- `none` and `star-delta` MAY be forced; a forced choice writes no `selection`.
- Basis for the threshold: star-delta reduced compressed size by about 25% on arid scenes and about 6% on vegetated scenes (2026-09-30). A plain store reads correctly in xarray, zarrita and zarr-layer without an adapter, so the extra decode path is worth carrying only where the saving is material.

## 5. Volatility

A float32 array at `volatility_path` (`volatility`) in the root group, shape `(grid_rows_0, grid_cols_0)`, one value per LOD 0 cell, single chunk.

```
volatility[r, c] = clip( mean(|source[t] - source[ref(t)]|
                              over all non-anchor t, all bands, all pixels of cell (r, c))
                         / 10000 , 0, 1)
```

- `ref(t)` is `delta_reference[t]` for `star-delta` stores. For `none` stores the writer evaluates the same expression against a nominal nearest-anchor schedule (anchor interval chosen by the writer, default 6); that schedule is not recorded and readers MUST NOT depend on it beyond ordering.
- Computed on exact differences (int32 for integer dtypes, float64 for `float32`), over all pixels including invalid ones, at LOD 0 only.
- `0.0` when the cell has no delta timesteps (`anchor_interval = 1` or `n_time = 1`).
- The divisor 10000 is a fixed normalization constant for every dtype (it is the Sentinel-2 reflectance scale). It is not a physical unit; for other sources the value is still monotone in temporal change.

Readers use it to order prefetch (volatile cells first) and to draw change overviews. A store without it is decodable but not conforming.

## 6. Multiscale pyramid

Each level halves pixel dimensions and doubles ground sample distance by block averaging that excludes invalid pixels (§2.3). A block whose pixels are all invalid yields `nodata` (`0` when `nodata` is `null`).

- Level 0: native resolution (10 m for Sentinel-2). Level `k`: resolution `* 2^k`.
- Level `k` is derived from level `k-1` by a factor-2 block mean. Before averaging, the source is padded to an even height and width by edge replication, so `H_k = ceil(H_{k-1} / 2) = ceil(H_0 / 2^k)`, same for `W`.
- The mean of integer dtypes (`uint8`, `uint16`, `int16`) is computed on the exact sum in a wider integer type (`uint32` or `int32`) with floor division by the valid-pixel count, and stored in the data dtype. The mean of `float32` accumulates in float64 and is stored as float32.
- `mask` reduces by maximum and `coverage` by rounded mean (§2.4, §2.5).
- Downsampling is applied to source timesteps; star-delta is then applied per level. Deltas are never downsampled.
- **Chunk size stays constant.** Only the grid shrinks: `grid_rows_k = ceil(H_k / cs)`.
- Levels MUST be consecutive from 0. The encoder default stops at the first level whose grid is 1 x 1 (that level is included). Fewer or more levels MAY be present.

## 7. Sharding

### 7.1 Layout

**Default: sharded.** The array uses the `sharding_indexed` codec with shard shape `(shard_time, n_band, cs, cs)` and inner chunk shape `(1, n_band, cs, cs)`. A shard object holds one spatial cell over `shard_time` consecutive timesteps, and one timestep is one HTTP byte-range read. Zarrita 0.7.5 read sharded stores over byte ranges with bytes transferred equal to the unsharded store and bit-exact reconstruction (spike, 2026-09-29).

```json
"chunk_grid": { "name": "regular", "configuration": { "chunk_shape": [4, 2, 512, 512] } },
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
- **`shard_time`** is an integer in `[1, n_time]`; the writer default is `n_time` (one shard holds a cell's whole time axis). The time grid has `n_shards_t = ceil(n_time / shard_time)` shards, so the shard grid of the data array is `(n_shards_t, 1, rows, cols)`. Readers MUST handle more than one shard along time.
- Timestep `t` lives in time shard `ts = floor(t / shard_time)` at inner position `t mod shard_time`. The shard key is `c/{ts}/0/{r}/{c}`. When `shard_time = n_time`, `ts` is always 0 and the keys are `c/0/0/{r}/{c}`, as in v0.1. `mask` and `coverage` have no band axis: shard shape `(shard_time, cs, cs)`, key `c/{ts}/{r}/{c}`.
- Objects per level drop from `n_time * grid_rows * grid_cols` to `n_shards_t * grid_rows * grid_cols`.
- The writer SHOULD choose `shard_time` so that no shard object exceeds the largest object the intended host or CDN will cache or range-serve, and, for `star-delta` stores, SHOULD choose a multiple of `anchor_interval`.
- **Both forms are valid.** Unsharded (§2.1) remains valid for hosts without byte-range support. A reader MUST handle both; it learns which from the `codecs` list.

### 7.2 Shard index

- The index holds one `(offset, nbytes)` uint64 pair per inner chunk of the shard: `16 * shard_time` bytes plus 4 bytes crc32c, `N = 16 * shard_time + 4`. A partial last time shard is stored at full shard shape: its index still has `shard_time` entries, and entries for timesteps `>= n_time` are empty.
- With `index_location: "end"` a reader fetches the index with the last `N` bytes of the shard; with `"start"`, the first `N` bytes. Readers MUST support both. Writers SHOULD use `"end"`: zarrita 0.7.5 ignores `index_location` and decodes start-indexed shards as garbage without error, which would break every stock zarrita reader including CarbonPlan zarr-layer.
- Without `shard_bytes` (§7.3), a reader fetches the end-located index with a suffix range (`Range: bytes=-N`, which triggers a CORS preflight) or learns the object length with a `HEAD` first. Zarrita issues one `HEAD` before the index read, so a cold cell costs `HEAD`, index, chunk, then one range per timestep.
- Readers MUST cache the index per shard and reuse one array handle per level (zarrita caches the index per array instance).
- Empty inner chunks (both index values `2^64 - 1`) decode as all `fill_value`. A shard object that does not exist (`404`) is an entirely empty shard, and readers MUST decode it as all `fill_value`; writers MAY omit shards that are entirely fill.

### 7.3 `shard_bytes`

Optional root attribute `chronozarr.shard_bytes`, sharded stores only, covering the data array (not `mask` or `coverage`):

```text
{ "<level path>": { "<t_shard>/<row>/<col>": <byte length of that shard object> } }
```

- It lists exactly the shard objects of the data array that exist, for every level.
- With the length of an end-located shard known, a reader issues the index read as the bounded range `bytes=(L-N)-(L-1)`, needing no `HEAD` and no preflight. Readers MUST use `shard_bytes` when present and MUST fall back to a `HEAD` for any shard it does not list.
- Because stores are immutable (§9), the lengths cannot go stale.

### 7.4 Star-delta across shards

A non-anchor timestep and its reference anchor can sit in different time shards (for example the last timesteps before a shard boundary whose nearest anchor is the first timestep of the next shard). Such a read still touches two chunks (Invariant 2) but two shards, so a cold read also pays the second shard's index. Choosing `shard_time` as a multiple of `anchor_interval` keeps almost every delta and its anchor in one shard.

## 8. Compression codec

| Codec | Status | Configuration |
|---|---|---|
| `zstd` | Default | `{ "level": 5, "checksum": false }` |
| `blosc` | Permitted | `cname` `zstd` or `lz4`; `shuffle` `noshuffle` or `shuffle`; `clevel` 0 to 9; `typesize` the element size in bytes of the data dtype; `blocksize` 0 |
| `gzip` | Permitted | `{ "level": 1..9 }` |

- Readers MUST support all three, including both `cname` values and both `shuffle` values of `blosc`. Writers MUST use exactly one, preceded by `bytes` little-endian.
- Other codecs, other `blosc` `cname` values and `shuffle: "bitshuffle"` MUST NOT be used: the browser reader carries no WASM dependency for them.
- The same codec chain applies to `data`, `mask` and `coverage`; the `time`, `band`, `x`, `y` and `volatility` arrays MAY use any of the three.

Rationale: size and browser decode speed were measured, not assumed. On one real Sentinel-2 chunk (4 x 512 x 512 uint16, 2,097,152 bytes, median of 15 in Chromium, 2026-09-29) zstd via the numcodecs-js WASM codec that zarrita uses decoded in 4.5 ms at 1,284,781 bytes, native gzip via `DecompressionStream` in 6.7 ms at 1,387,409 bytes, and zstd via the pure-JS fzstd in 14.1 ms. On four real Ucayali LOD 0 chunks of 2,097,152 bytes (zarrita 0.7.5 with vendored codec modules served locally, headless Chrome, median of 20, 2026-09-30), zstd level 5 stored 1,376,469 bytes and `blosc` with zstd level 1 and byte shuffle stored 1,520,154 bytes, 10.4% more. Steady-state decode took 4.4 ms for zstd and 3.5 ms for `blosc` (4.4 and 3.3 ms inside a worker), and every reconstruction was bit-exact. Byte shuffle did not reduce the size of the zstd stream on this data. The 10.4% size penalty alone decides the default: zstd level 5 stores the fewest bytes, and the decode difference is about 1 ms per chunk. `blosc` stays permitted for writers that prefer encode speed or a faster steady-state decode, and `gzip` for writers without a zstd implementation. Encode cost is irrelevant to the default.

## 9. Static hosting requirements

A chronozarr store is served by any HTTP server or object store that returns files by path. No server-side code. Host recipes and a header checklist are in `docs/hosting.md`.

| Requirement | Level |
|---|---|
| `GET {store}/{key}` returns the object bytes; an absent key returns `404` (not a fallback page with `200`) | MUST |
| `Range` requests honoured with `206 Partial Content` and `Content-Range` (needed for sharded stores; unsharded stores need only GET) | MUST for sharded |
| Chunk and shard objects served as stored: no `Content-Encoding` or other transformation, so byte offsets match the shard index | MUST |
| `Access-Control-Allow-Origin: *` | MUST |
| `Access-Control-Allow-Headers: Range` (or `*`) and a `200`/`204` answer to `OPTIONS` preflight | SHOULD |
| `Access-Control-Expose-Headers: Content-Range, Content-Length` | SHOULD |
| `Cache-Control: public, max-age=31536000, immutable` | SHOULD |
| `Timing-Allow-Origin: *` | MAY |

- Browsers do not preflight a bounded `Range: bytes=a-b`; only suffix ranges `bytes=-N` trigger one. A reader that uses `shard_bytes` (§7.3) issues only bounded ranges.
- Without `Timing-Allow-Origin`, `PerformanceResourceTiming.transferSize` is 0 for cross-origin requests, so a reader can count bytes only from `Content-Length`.
- Stores are treated as immutable. A re-encode MUST be written under a new prefix, never in place.
- Upload order SHOULD be: all chunk and shard objects, then group and array `zarr.json` below the root, then the root `zarr.json` last. A reader treats the root `zarr.json` as the marker that the store exists, so it never describes missing data.
- Zarr v3 metadata is `zarr.json`, never a dotfile, so hosts that hide dotfiles (GitHub Pages, some CDNs) serve a store correctly. No `.zarray`, `.zattrs` or `.zmetadata` exist.
- Directory listing is never required (a reader derives every key from metadata) and object content types are ignored.

## 10. Reader requirements

A conforming reader MUST:

1. `GET {store}/zarr.json`. Reject the store if `attributes.chronozarr.spec_version` is not `0.1.x` or `0.2.x`, or if `temporal.encoding` is not `"none"` or `"star-delta"` (a v0.1 store is `star-delta`). Take timestamps from `chronozarr.times`, band objects from `chronozarr.bands` (strings read as `{ name }`), the data array name from `chronozarr.variable`, and the validity rule inputs from `chronozarr.nodata`, `mask_variable` and `coverage_variable`.
2. Enumerate levels from `chronozarr.levels` when present; otherwise from `multiscales[0].datasets[].path`, reading each level's `zarr.json` (`transform`, `resolution`). Read `{variable}/zarr.json` (`shape`, `data_type`, `chunk_grid`, `codecs`) for each level, or take them from consolidated metadata when present. Fail with an error naming any unsupported `data_type` or codec.
3. Select a level: the largest `k` whose `resolution` does not exceed the requested output ground sample distance; `k = 0` if none.
4. For timestep `t` and cell `(r, c)`: under `none`, or when `t` is in `anchor_indices`, read one chunk. Otherwise read the chunk at `delta_reference[str(t)]` and the chunk at `t`. Never more than two reads (Invariant 2).
5. Reconstruct per §4.2: unsigned wraparound add in the stored dtype, no clamp. Discard elements beyond `shape` (§2.1).
6. For sharded arrays, compute the shard and inner position from `shard_time` (§7.1), read the shard index (§7.2, using `shard_bytes` when present), then one byte range per inner chunk. Decode a missing shard as `fill_value`.
7. Apply the validity rule (§2.3): invalid pixels are missing in band math and statistics. Read `{level}/mask` and `{level}/coverage` only when `mask_variable` and `coverage_variable` are present.
8. Apply `scale` and `offset` (§3.6) wherever physical values are shown or combined.

A reader SHOULD cache decoded anchors and shard indices for the session, and SHOULD prefetch anchors before deltas: once every anchor in the viewport is resident, any timestep costs one delta read. Under `none` there are no deltas and prefetch order is free.

Reference implementations: the Python package `chronozarr` (`open_store(path_or_url)` returns a reader with `read(t)`, `read_cell(t, row, col)` and `to_xarray()`; `validate(store)` checks a store against this spec) and the JavaScript reader `js/chronozarr/decoder.js` (zarrita store, reconstruction, cache, prefetch), DOM-free so it runs in a Worker, under MapLibre, or on a bare canvas.

A plain Zarr reader (`xarray.open_zarr(store, group="0")`, zarrita) reads a `none` store without this spec. For a `star-delta` store it reads anchor timesteps correctly and non-anchor timesteps as residuals.

## 11. What this is not

- Not a new binary format. It is Zarr v3 with a layout convention.
- Not a codec. Star-delta spans chunks, so it cannot sit in a Zarr `codecs` list; it lives in group attributes and the reader.
- Not a video codec. Deltas are block-compressed arrays, not I/P/B frames.
- Not a spatial index. A regular grid of chunks with a power-of-2 pyramid.
- Not a server or an API. There is nothing to run; a bucket is the deployment.
- Not a viewer. TileRipper is one consumer; zarr-layer and xarray are others.

## 12. Relationship to prior art

A row-by-row comparison with guidance on when to choose each tool is in `docs/format-comparison.md`.

- **PMTiles** (Protomaps): single-file archive of z/x/y tiles for static hosting. The hosting model (one bucket, range reads, no server) is the same. Tiles are images; per-tile values are whatever the image encoding carries, and there is no native time axis.
- **Mapbox raster-array (MRT)**: multi-band numeric raster tiles with a time-like band dimension, decoded client-side. The decoder code is published in mapbox-gl-js (`src/data/mrt`); the format is produced by the Mapbox Tiling Service and consumed by Mapbox's renderer, so using it means using that service and renderer. chronozarr is a Zarr-based alternative that a static bucket serves.
- **carbonplan ndpyramid + zarr-layer**: Zarr pyramids rendered in MapLibre with a time selector. chronozarr writes the same `multiscales` attribute and level/variable shape, so zarr-layer can read a `none` store as-is. zarr-layer reads the `proj` and `spatial` attributes and supports arbitrary CRS through proj4 reprojection. A `star-delta` store needs an adapter that reconstructs the residuals before zarr-layer renders it; stock zarr-layer does not. chronozarr stores stay in a projected per-AOI CRS and declare it via `proj`/`spatial` attributes.
- **GeoZarr** and the zarr-conventions `proj`/`spatial`/`multiscales` drafts: CRS and affine conventions chronozarr aligns with (`crs`, `transform`, `proj:code`, `spatial:*`). GeoZarr's multiscale layout is still a proposal. chronozarr adds the temporal block, the band objects, the `times`/`band_names`/`levels` mirrors, `mask`, `coverage` and the volatility array on top.
- **COG + TiTiler**: single-timestep GeoTIFFs rendered by a tile server. It needs a running server and one request path per timestep; chronozarr needs neither.

## 13. Changes from 0.1

- **Version.** `spec_version` is `0.2.0`. Readers accept `0.1.x` and `0.2.x`; every v0.1 store is a valid v0.2 store (its `bands` is a list of strings, it has no `levels`, `band_names`, `mask_variable` or `selection`, and its residual bytes are valid modular residuals). A v0.1-only reader rejects `0.2.x` stores at the version check; the `multiscales` attribute, `dimension_names` and plain arrays keep plain Zarr consumers working.
- **Temporal encoding is optional** (§4). `temporal.encoding` is `none` or `star-delta`. The writer default `auto` measures a sample of LOD 0 cells and keeps star-delta only at 0.85 of the plain size or better, recording `temporal.selection`.
- **Residuals are modular** (§4.2). The clip and the encoder's overflow failure are gone; decoders wrap instead of clamping. Bytes are identical to v0.1 wherever the difference fits the signed range. `uint8` joins `uint16` as a star-delta dtype.
- **Codecs** (§8). Readers must support `zstd`, `gzip` and `blosc` (`cname` zstd or lz4, `shuffle` or `noshuffle`). The default stays `zstd` level 5.
- **dtype and nodata profiles** (§2.3). `uint8`, `uint16`, `int16` and `float32`; `nodata` is a number or `null`; optional `mask` variable (§2.4). `int16` and `float32` are `none` only.
- **Band objects** (§3.6). `bands` becomes a list of `{ name, common_name?, scale?, offset?, units? }` with a `band_names` string mirror; physical value is `stored * scale + offset`.
- **Coverage and provenance** (§2.5, §3.8). Optional `coverage` variable and `provenance` object.
- **Layout options and length hints** (§7, §3.7). `chunk_size` (256 or 512 recommended); `shard_time` and a multi-shard time grid; `shard_bytes`; `levels`.
- **Volatility** (§5). Written for every store, `none` included, with a fixed divisor of 10000 for every dtype.
- **Hosting** (§9). `404` for absent keys, no `Content-Encoding` on chunk and shard objects, `Timing-Allow-Origin` as a MAY, a missing shard object decodes as fill, upload order with the root `zarr.json` last.
- **GDAL CRS** (§3.3). The data array carries `_CRS` for EPSG stores so GDAL assigns the CRS.
- **Prior art** (§12). Corrected statements about Mapbox raster-array and zarr-layer; COG + TiTiler added.
- **Unchanged.** Zarr v3 groups per level, `multiscales` in the ndpyramid form, `dimension_names` everywhere, consolidated metadata, native CRS, sharded default with the index at the end, the anchor schedule, the pyramid geometry.
