# v0 Native Chunk Spec

**Status:** Draft
**Date:** 2026-04-15

This spec defines the native storage and access contract for the multiband
basemap runtime. It replaces the experimental encoding zoo with a single
production-oriented format.

## Design center

The system is a **multiband basemap runtime**, not a spacetime codec. The
stored unit is raw reflectance. Products are derived on demand. Temporal delta
encoding is an optional sidecar, not the core primitive.

## 1. Chunk geometry

| Property | Value |
|----------|-------|
| Default chunk size | 512 px (5.12 km at 10m) |
| Low-latency option | 256 px (2.56 km at 10m) |
| Coordinate system | UTM per AOI (never Web Mercator for storage) |
| Addressing | `r{row:03d}_c{col:03d}` |
| Edge behavior | Partial chunks at mosaic edges (< chunk_size) |

The chunk grid is deterministic for a given AOI extent and chunk size.

## 2. Band packing

Each chunk stores 4 bands in fixed order:

| Index | Band | Wavelength | Resolution |
|-------|------|-----------|-----------|
| 0 | B02 | Blue (490 nm) | 10m |
| 1 | B03 | Green (560 nm) | 10m |
| 2 | B04 | Red (665 nm) | 10m |
| 3 | B08 | NIR (842 nm) | 10m |

- Dtype: `uint16` (surface reflectance x 10000, range [0, 10000])
- No-data: 0 (valid reflectance floors at ~100)

Future extension: additional bands (B05-B07, B8A, B11, B12) would change the
band count but not the format. The band index is part of the manifest.

## 3. Month index

Time is discrete monthly steps. Each chunk stores a time-stacked array:

```
shape: (n_months, 4, chunk_h, chunk_w)
chunks: (1, 4, chunk_h, chunk_w)    # per-month independence
dtype: uint16
compressor: Blosc(zstd, clevel=5, BITSHUFFLE)
```

Zarr chunk layout `(1, 4, H, W)` means each month decompresses independently.
This is the B2_chunked layout from the experiment.

Month labels are ISO-format strings: `["2023-01", "2023-02", ..., "2024-12"]`.
The mapping from time index `t` to month label is in the manifest.

## 4. On-disk layout

```
{store_root}/{aoi}/
  manifest.json
  chunks/
    r000_c000/
      stack.zarr          # (n_months, 4, H, W) uint16
    r000_c001/
      stack.zarr
    ...
```

### manifest.json

```json
{
  "version": "0.1.0",
  "aoi": "sahara_tamanrasset",
  "label": "Sahara (Tamanrasset, Algeria)",
  "epsg": 32631,
  "transform": [10.0, 0.0, 599700.0, 0.0, -10.0, 2541200.0],
  "mosaic_height": 2814,
  "mosaic_width": 2616,
  "chunk_size": 512,
  "n_rows": 6,
  "n_cols": 6,
  "bands": ["B02", "B03", "B04", "B08"],
  "dtype": "uint16",
  "scale_factor": 10000,
  "nodata": 0,
  "months": ["2023-01", "2023-02", "...", "2024-12"],
  "source": "Sentinel-2 L2A via Planetary Computer",
  "composite_method": "monthly median, SCL cloud mask, carry-forward gap fill"
}
```

## 5. Product derivation contract

Products are computed at serve time from the raw multiband state. The render
module is the single source of truth for band math.

| Product | Formula | Output |
|---------|---------|--------|
| `true_color` | RGB(B04, B03, B02) + percentile stretch | uint8 (H, W, 3) |
| `false_color` | RGB(B08, B04, B03) + percentile stretch | uint8 (H, W, 3) |
| `ndvi` | (B08 - B04) / (B08 + B04) | float32 (H, W) |
| `ndwi` | (B03 - B08) / (B03 + B08) | float32 (H, W) |
| `ndvi_rgb` | NDVI mapped to brown-green colormap | uint8 (H, W, 3) |

Adding a new product means adding one function to `render.py`. No data
re-encoding required.

## 6. Access patterns

### Single tile request
```
GET chunk_id + month_index + product
  → decompress 1 Zarr chunk: (1, 4, H, W) uint16
  → apply band math for product
  → encode as PNG/WebP
```

### Viewport (3x3 chunks)
```
GET 9 chunk_ids + month_index + product
  → 9 independent Zarr chunk reads (parallelizable)
  → 9 renders
  → stitch or serve individually
```

### Product switch (same viewport, same month)
```
  → 0 bytes fetched (bands already in memory/cache)
  → re-render with new band math
```

### Time scrub (same viewport, next month)
```
  → 9 new Zarr chunk reads (month t+1)
  → 9 renders
  (no dependency on previous month)
```

## 7. Compatibility tile path (XYZ/WMTS)

For standard web map consumption, generate XYZ tiles from native chunks:

1. Read native chunk + month
2. Reproject from UTM to Web Mercator (EPSG:3857)
3. Render product
4. Slice into 256x256 XYZ tiles
5. Serve as PNG with standard `/{z}/{x}/{y}.png` URL scheme

This is a **compatibility output layer**, not a storage format. The native
chunks remain the source of truth.

Not implemented in v0. Planned for v1.

## 8. Optional delta extension

For AOIs with known low temporal entropy (deserts, open ocean, stable
infrastructure), an optional delta sidecar can reduce storage by ~14%:

```
{chunk_id}/
  stack.zarr              # full monthly state (primary)
  delta/
    meta.json             # keyframe schedule
    keyframes/{month}.zarr  # uint16 full state
    deltas/{month}.zarr     # int16 residuals
```

This is strictly optional. The primary access path always reads from
`stack.zarr`. Delta encoding is a storage optimization that trades decode
complexity for modest compression gains in favorable regimes.

**Do not use for:** landscapes with seasonal variability (cropland, deciduous
forest, snow zones).

## 9. Decisions and non-goals

| Decision | Rationale |
|----------|-----------|
| UTM storage, never Web Mercator | Preserves area/distance for analysis |
| Monthly composite granularity | Matches cloud-free availability of S2 |
| No per-product pre-rendering | Core architectural win (H1) |
| No sub-chunk spatial tiling | 512px is small enough for viewport ops |
| Zarr v2, not v3 | numcodecs compatibility (pinned <0.15) |
| zstd + BITSHUFFLE | Best ratio for uint16 reflectance data |

## 10. Migration from experimental stores

The existing `baseline_b/b2_chunked/` stores **are** the v0 format, minus the
manifest. Migration:

1. Generate `manifest.json` from mosaic metadata + chunk grid
2. Rename `baseline_b/b2_chunked/{chunk_id}/stack.zarr` → `chunks/{chunk_id}/stack.zarr`
3. Done. No data re-encoding required.
