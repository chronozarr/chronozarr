# ChronoFabric v1 Format Specification

**Status:** Frozen
**Date:** 2026-04-18
**Branch:** `feat/chronofabric`

---

## Invariants

1. **Lossless roundtrip.** `encode_v1(data) → decode_v1()` returns the exact input uint16 values. No quantization, no rounding, no lossy compression.
2. **O(1) random month access.** Any month decodes with at most 2 reads: one anchor + one delta. No chain accumulation, no sequential dependency.
3. **uint16 preservation.** Band values are stored and served as unsigned 16-bit integers (Sentinel-2 reflectance × 10000). No conversion to float or 8-bit at any point in the storage/serving path.
4. **Self-describing stores.** Each store contains a `manifest.json` that fully describes the layout, temporal encoding, and chunk grid. No external metadata required.

---

## 1. On-disk layout

```
{store_dir}/
  manifest.json
  lod/
    0/chunks/
      r000_c000/stack.zarr/     # Zarr v2 directory store
        .zarray                 # shape (n_months, 4, H, W), dtype uint16
        0.0.0.0                 # chunk file: month 0, all bands, full spatial
        1.0.0.0                 # chunk file: month 1
        ...
      r000_c001/stack.zarr/
      ...
    1/chunks/
      r000_c000/stack.zarr/     # downsampled 2×
      ...
    2/chunks/
      ...
```

Each `stack.zarr` is a Zarr v2 directory store with:
- **Shape:** `(n_months, 4, H, W)` — time × bands × height × width
- **Chunks:** `(1, 4, H, W)` — each month is one independently-addressable chunk
- **Dtype:** `uint16` (anchors store true reflectance; deltas store int16 reinterpreted as uint16)
- **Compressor:** `numcodecs.Zstd(level=5)` — no Blosc wrapper

### Chunk naming

Zarr chunk files are named `{month_index}.0.0.0` (time.bands.row.col). Since chunks span the full band/spatial extent per time step, the last three indices are always 0.

### Chunk ID convention

Grid cells are named `r{row:03d}_c{col:03d}`. Row 0 is the top (north) edge of the mosaic.

### Edge chunks

Edge chunks (right column, bottom row) may be smaller than `chunk_size`. Their actual pixel dimensions are determined by the mosaic size:
- Interior chunk: `chunk_size × chunk_size`
- Right edge: `chunk_size × (mosaic_width - (grid_cols - 1) * chunk_size)`
- Bottom edge: `(mosaic_height - (grid_rows - 1) * chunk_size) × chunk_size`
- Corner: remaining pixels in both dimensions

---

## 2. Star-delta temporal encoding

### Algorithm

Given `n_months` and `anchor_interval`:

1. **Anchor indices:** `[0, anchor_interval, 2*anchor_interval, ...]`
2. **Delta reference:** each non-anchor month references the nearest anchor (by absolute distance). Ties break toward the earlier anchor.
3. **Anchor storage:** `stack[t] = mosaic[t]` as uint16
4. **Delta storage:** `stack[t] = (mosaic[t].int32 - anchor.int32).clip(-32768, 32767).int16.view(uint16)`

### Decoding

```
if t is anchor:
    return stack[t]                                    # uint16, use directly
else:
    anchor_t = manifest.temporal.delta_reference[t]
    anchor   = stack[anchor_t]                         # uint16
    delta    = stack[t].view(int16)                     # reinterpret
    return (anchor.int32 + delta.int32).clip(0, 65535).uint16
```

### Why star, not chain

Chain-delta (each month references the previous) gives ~10-15% better compression but requires sequential decoding. Star-delta gives O(1) random access to any month — critical for scrubbing.

---

## 3. Multiscale pyramid

Each LOD level halves pixel dimensions and doubles ground resolution via area-weighted block averaging (excluding nodata=0 pixels).

- LOD 0: native resolution (e.g., 10m for Sentinel-2)
- LOD k: resolution × 2^k

The grid shrinks at each level. **Chunk size stays constant** (default 512px) — only the grid dimensions change. The number of LOD levels depends on the mosaic size; encoding stops when the grid shrinks to 1×1.

Downsampling pads the mosaic to a multiple of the factor using edge replication before block averaging. This ensures integer grid dimensions at each level.

---

## 4. Manifest schema

```json
{
  "version": "1.0.0",
  "epsg": 32631,
  "transform": [10.0, 0.0, 746090.0, 0.0, -10.0, 2540440.0],
  "mosaic_height": 2814,
  "mosaic_width": 2616,
  "bands": ["B02", "B03", "B04", "B08"],
  "dtype": "uint16",
  "nodata": 0,
  "months": ["2024-01", "2024-02", "2024-03"],
  "compressor": "zstd",
  "compressor_level": 5,
  "lod_levels": 5,
  "lod_factor": 2,
  "temporal": {
    "encoding": "star-delta",
    "anchor_interval": 2,
    "anchor_indices": [0, 2],
    "delta_reference": { "1": 0 }
  },
  "lods": [
    { "level": 0, "resolution_m": 10.0, "grid_rows": 6, "grid_cols": 6, "chunk_size": 512 },
    { "level": 1, "resolution_m": 20.0, "grid_rows": 3, "grid_cols": 3, "chunk_size": 512 },
    { "level": 2, "resolution_m": 40.0, "grid_rows": 2, "grid_cols": 2, "chunk_size": 512 },
    { "level": 3, "resolution_m": 80.0, "grid_rows": 1, "grid_cols": 1, "chunk_size": 512 },
    { "level": 4, "resolution_m": 160.0, "grid_rows": 1, "grid_cols": 1, "chunk_size": 512 }
  ],
  "cells": {
    "r000_c000": { "volatility": 0.0094 },
    "r000_c001": { "volatility": 0.0101 }
  }
}
```

### Field definitions

| Field | Type | Description |
|-------|------|-------------|
| `version` | string | Always `"1.0.0"` for v1 stores |
| `epsg` | int | CRS EPSG code (UTM zone, never Web Mercator) |
| `transform` | float[6] | Affine transform coefficients `[a, b, c, d, e, f]` (rasterio convention) |
| `mosaic_height` | int | Full mosaic height in pixels at LOD 0 |
| `mosaic_width` | int | Full mosaic width in pixels at LOD 0 |
| `bands` | string[] | Band names in array order |
| `dtype` | string | Always `"uint16"` |
| `nodata` | int | Nodata value (always 0) |
| `months` | string[] | Ordered list of YYYY-MM month strings |
| `compressor` | string | Compression algorithm (`"zstd"`) |
| `compressor_level` | int | Compression level (5) |
| `lod_levels` | int | Number of LOD levels (including LOD 0) |
| `lod_factor` | int | Downsampling factor per level (always 2) |
| `temporal.encoding` | string | Always `"star-delta"` |
| `temporal.anchor_interval` | int | Months between anchors |
| `temporal.anchor_indices` | int[] | Month indices that are anchors |
| `temporal.delta_reference` | dict[str,int] | Month index (as string) → anchor index it references |
| `lods[].level` | int | LOD level number (0 = native) |
| `lods[].resolution_m` | float | Ground sample distance in meters |
| `lods[].grid_rows` | int | Number of chunk rows at this LOD |
| `lods[].grid_cols` | int | Number of chunk columns at this LOD |
| `lods[].chunk_size` | int | Target chunk size in pixels (always 512) |
| `cells` | dict | Per-cell metadata keyed by chunk ID (LOD 0 only) |
| `cells[].volatility` | float | Mean absolute delta / 10000, in [0, 1]. Higher = more temporal change |

---

## 5. API contract

### `GET /v1/manifest/{aoi}`

Returns manifest JSON. Cache: 1 hour.

### `GET /v1/chunk/{aoi}/{lod}/{chunk_id}/{month_index}`

Returns raw Zarr chunk bytes (already Zstd-compressed by the encoder). The client decompresses with Zstd, yielding a flat `uint16[4 * H * W]` array in C order (bands × height × width).

Response headers:
- `X-Chunk-Encoding: zstd`
- `X-Chunk-Height: {H}`
- `X-Chunk-Width: {W}`
- `Cache-Control: public, max-age=86400, immutable`

The server does **not** render, reproject, reconstruct deltas, or transcode. It serves raw chunk bytes.

### v0 endpoints (preserved)

`/v1/tiles/`, `/v1/query/`, `/v1/stats/` continue to work for v0 stores. They are server-side rendered and independent of v1.

---

## 6. Browser runtime (implemented)

The current browser viewer (`static/viewer.html`) implements:

- **Zstd decode:** `fzstd.js` (3.8 KB pure JS ESM module, bundled locally)
- **Star-delta reconstruction:** fetch anchor + delta, add in JS, clip to uint16
- **WebGL2 rendering:** R16UI textures (one per band), `texelFetch` in fragment shader
- **True color shader:** reflectance → highlight compression → gamma → saturation → sRGB
- **Multi-chunk stitching:** fetches all grid chunks in parallel, renders each to correct `gl.viewport` position
- **LOD switching:** dropdown selects pyramid level, canvas resizes to mosaic dimensions at that LOD
- **Month navigation:** prev/next buttons + arrow keys
- **In-memory chunk cache:** `Map` keyed by `lod/chunkId/monthIndex`, eliminates re-fetches
- **Anchor cache metrics:** tracks anchor hits/misses (session-wide), resident count, bytes held, warm fraction per frame
- **Adjacent-month prefetch:** after rendering, prefetches month±1 for all visible chunks with limited concurrency (4 parallel fetches), auto-cancels on navigation

### What the browser does NOT do (yet)

- No web workers (decode runs on main thread)
- No OPFS / persistent caching (cache lives in memory only, lost on page reload)
- No smooth temporal interpolation (month-step only)
- No WebGPU (WebGL2 only)
- No spatial panning/zoom within the canvas

These are Phase 2 optimizations. The current path proves the architecture.

---

## 7. Decisions (frozen 2026-04-18)

| # | Question | Decision | Rationale |
|---|----------|----------|-----------|
| 1 | Zarr version | v2 | v3 sharding immature; v2 chunk files work for individual serving |
| 2 | Delta topology | Star (not chain) | O(1) random access; 10-15% compression loss is acceptable |
| 3 | Pyramid scheme | Powers of 2 from native | Standard, interoperable, simple |
| 4 | GPU API | WebGL2 primary | WebGPU deferred to Phase 2 |
| 5 | Time interaction | Month-step | Smooth scrubbing deferred to Phase 2 |
| 6 | Browser codec | Plain Zstd via fzstd | 16ms decode, 3.8 KB, no WASM. Blosc's 12% size win doesn't justify 150 KB WASM dep |

### Benchmark data (from `static/bench.html`)

| Codec | Decode (ms) | Compression ratio | Browser dependency |
|-------|------------|-------------------|-------------------|
| Zstd (fzstd) | 16 | 1.33× | pure JS, 3.8 KB |
| gzip (native) | 7.8 | 1.34× | DecompressionStream |
| star-delta (2× Zstd + add) | 29 | 1.65× | fzstd + loop |

Star-delta total is 29 ms cold, ~17 ms warm (anchor cached).

---

## 8. What this is NOT

- Not a new binary format. It is Zarr v2 with a specific layout convention.
- Not a video codec. Deltas are block-compressed arrays, not I/P/B frames.
- Not a spatial indexing innovation. Grid-of-chunks, same as v0.
- Not a server-side rendering system. The browser does all rendering.

The novelty is: **star-delta temporal encoding in Zarr + browser-side Zstd decode + GPU rendering from raw uint16 bands.**
