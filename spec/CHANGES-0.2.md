# chronozarr v0.2 change list (implementation contract, 2026-09-30)

Source of truth for the v0.2 work. The docs agent folds this into SPEC.md; implementers build against it.
Every v0.1 store remains a valid v0.2 store. `chronozarr.spec_version` becomes "0.2.0" for new stores.

## 1. Temporal encoding is optional; the writer chooses automatically
- `chronozarr.temporal.encoding` is `"none"` or `"star-delta"`. Readers MUST support both. With `"none"` the data array holds true values and readers do nothing.
- Writer default is `encoding="auto"`: encode a sample of LOD 0 cells (at least 3, or all if fewer) both ways and keep star-delta only if its compressed bytes are at most 0.85 x the plain bytes. Record `temporal.selection = {"mode": "auto", "sampled_cells": n, "ratio": r}`. `"none"` and `"star-delta"` may be forced.
- Rationale: measured saving is ~25 % on arid scenes and ~6 % on vegetated ones; plain stores are correct in xarray, GDAL and zarr-layer without an adapter.

## 2. Residuals are modular
- star-delta residual = (value - anchor) mod 2^dtype_bits, stored in the data dtype; reconstruction = (anchor + residual) mod 2^bits. No clipping, no rejection. Bit-identical to v0.1 bytes when |difference| < 2^(bits-1). Allowed for uint16 and uint8 only.

## 3. Codec
- Readers MUST support `zstd`, `gzip`, and `blosc` (cname zstd or lz4, shuffle or noshuffle).
- Writer option `--codec zstd|blosc-zstd-shuffle` and `--level`. The default becomes `blosc` (zstd, clevel 1, byte shuffle) ONLY if the js-reader measurement shows browser decode <= 5 ms per 2 MB chunk in zarrita; until that report, the default stays `zstd` level 5. Measured on Ucayali chunks: shuffle + zstd L1 is 10.6 % smaller than plain L5 and 3.9x faster to encode.

## 4. Dtype and nodata profiles
- Data dtype in {uint8, uint16, int16, float32}. star-delta only for uint8/uint16; int16 and float32 are `"none"` only.
- `chronozarr.nodata` is a number or null. Optional `mask` variable (uint8, 1 = valid) with dims (time, y, x), chunked like `data`, at every level (mean-reduced levels store 1 if any valid). Readers prefer `mask` when present.

## 5. Band metadata
- `chronozarr.bands` becomes a list of objects: `{ "name", "common_name"?, "scale"?, "offset"?, "units"? }`. Physical value = stored * scale + offset (defaults 1 and 0). Keep a mirror `chronozarr.band_names` (strings) for v0.1 readers and zarr-layer; readers accept either form.
- Viewer products key on `common_name` (red, green, blue, nir, swir16, swir22, ...) with fallback to `name`; stretch and index math use scale/offset, never a hardcoded 10000. An RGB uint8 store (bands red, green, blue, scale 1) renders as-is.

## 6. Coverage and provenance (optional)
- Variable `coverage` (time, y, x) uint8 = number of valid observations behind each pixel (0 = gap-filled or missing), chunked like `data`, every level (mean, rounded). `chronozarr.provenance` = `{ "sources": [...collection ids or URLs], "composite": "monthly median", "gap_fill": "carry-forward" | "none", "notes"? }`.
- Viewer: chart marks coverage-0 points as gap-filled; optional overlay hatches gap-filled pixels.

## 7. Layout options and length hints
- `chunk_size` in {256, 512} (default 512). Shard time extent `shard_time` <= n_time (default n_time). Readers MUST handle several shards along time: the shard grid is (ceil(n_time/shard_time), 1, rows, cols).
- Optional root attr `chronozarr.shard_bytes`: `{ "<level>": { "<t_shard>/<row>/<col>": byte_length } }` so a reader can range-read an end-located shard index without a HEAD. Readers use it when present and fall back to HEAD otherwise.
- Root attr `chronozarr.levels`: `[ { "path", "resolution", "transform", "shape": [t, b, y, x], "grid": [rows, cols] } ]`, mirroring level attrs so a reader opens with one GET.

## 8. Unchanged
- Zarr v3 groups per level, `multiscales` in the ndpyramid form (GeoZarr's layout is still a proposal; revisit when stable), `dimension_names` everywhere, consolidated metadata, native CRS, static hosting rules, sharded default with the index at the end.

## 9. JS reader/viewer API contract (js-reader provides, js-viewer consumes; js-viewer never edits js/chronozarr)
- `openStore(url, opts)` unchanged. `store.attrs` exposes `bands` (objects), `bandNames`, `nodata`, `encoding`, `levels`, `hasMask`, `hasCoverage`, `provenance`.
- `store.getCell(lod, row, col, t)` unchanged; `store.getCoverage(lod, row, col, t)` and `store.getMask(...)` (null when absent).
- `store.bandwidthEstimate()` bytes/s EWMA of recent transfers; `store.setBudgets({ decodedBytes, compressedBytes, speculativeBytesInitial })`; `store.prefetch(..., { seek: true })` for big jumps; `store.stats()` gains `compressedCacheBytes`, `speculativeBytes`, `dedupedRequests`.
- `store.getCoarseFrame(lod, cells, t)` fetches the visible cells of one level at demand priority and resolves when all are decoded (used for coarse-first cold open).
