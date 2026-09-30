# TileRipper

Open a decade of analysis-ready satellite time series in a browser tab from a static bucket. Scrub it like video. Click for real numbers.

TileRipper is the viewer. **chronozarr** is the open format underneath it: a Zarr v3 layout convention for raster time series with star-delta temporal encoding and a multiscale pyramid, readable by xarray and by any Zarr client, and designed so a browser can decode raw uint16 bands and render products on the GPU with no server.

Spec: [spec/CHRONOZARR.md](spec/CHRONOZARR.md)

## What is in this repo

| Part | Path | What it does |
|------|------|--------------|
| Format spec | `spec/CHRONOZARR.md` | Normative layout, attrs, star-delta, pyramid, hosting rules |
| Python package `chronozarr` | `src/chronozarr/` | `encode()` from an xarray DataArray, `open_store()` reader, CLI `chronozarr encode / validate / info` |
| JS decoder | `js/chronozarr/decoder.js` | DOM-free reader on top of zarrita: cells by (lod, row, col, t), cache, anchors-first prefetch |
| TileRipper viewer | `js/tileripper/` | WebGL2 viewer: time scrub, GPU product switching, click-to-query values |
| Ingest example | `examples/sentinel2_pc/` | Monthly Sentinel-2 median composites from Planetary Computer |

## Why another format

- **PMTiles** packs finished map tiles for one moment. No time axis, no values, Web Mercator only.
- **Mapbox raster-array** (MRT) is multi-band numeric tiles with a time series, but the decoder is proprietary and MapLibre has no open equivalent.
- **CarbonPlan ndpyramid + zarr-layer** put Zarr pyramids in MapLibre with a time selector, but every timestep is a fresh fetch, and the tiled path requires EPSG:4326 or 3857.

chronozarr keeps native projection and lossless uint16, compresses across time so scrubbing is cheap, and pre-stages the time axis in the client so a month switch costs zero bytes on the wire. Stores carry ndpyramid `multiscales` attrs and Zarr v3 `dimension_names`, so zarr-layer and xarray can read them as plain Zarr; a chronozarr-aware reader is only needed to reconstruct non-anchor timesteps.

## Quickstart

```bash
uv add chronozarr
```

```python
import chronozarr
chronozarr.encode(da, "my_store", chunk_size=512, anchor_interval=6)   # da: (time, band, y, x) uint16
store = chronozarr.open_store("my_store")
store.read(t=42)                                                         # (band, y, x) uint16, lossless
store.to_xarray(lod=0)
```

Serve the store from any static host that supports GET, byte ranges, and CORS (S3, R2, a local range-capable server), then open the viewer:

```
js/tileripper/index.html?store=https://your-bucket/my_store
```

## Measured

Browser decode of one real Sentinel-2 chunk (4 x 512 x 512 uint16, 2 MB raw), median of 15, 2026-09-29:

| Codec | Bytes | Decode |
|-------|------:|-------:|
| zstd via zarrita (WASM) | 1,284,781 | 4.5 ms |
| gzip via native DecompressionStream | 1,387,409 | 6.7 ms |
| zstd via fzstd (pure JS) | 1,284,781 | 14.1 ms |

Viewer on the v0.1 Sahara store (128 months, 4 bands, 6 x 6 cells at LOD 0, 3.9 GB in 93 files), localhost, HTTP cache bypassed, 2026-09-29:

| Measurement | Real-GPU Chromium, 36-cell 12-month stress store | Built-in browser pane, full 128-month Sahara store |
|-------------|------:|------:|
| Cold open to first complete frame | 257 ms, 109 requests, 43.8 MB | 475 ms median, 109 requests, 35.2 MB |
| Cold open without consolidated metadata | 256 ms | 556 to 1478 ms |
| Warm timestep switch, stepping (median / p95) | 3.5 / 15 ms | 6.3 / 19 ms |
| Warm switch wire bytes and requests | 0 / 0 | 0 / 0 |
| Product switch | about 1 ms | under 1 ms |
| Decode per 2 MB chunk | 4.6 ms | 10 ms under load (4.5 ms idle) |

Star-delta reconstruction runs in the fragment shader; the CPU loop it replaces cost 54 ms per 36-cell frame. Known limits: on long time axes the anchors-first prefetch fills the decoded cache with anchors before reaching deltas, and decode still runs on the main thread. Both are open items, not format changes.

## Status

v0.1 draft. Layout is Zarr v3 groups per level, sharded by default so one file per spatial cell holds the whole time axis and a timestep is one range read. The demo stores (Sahara, arid; Iowa, seasonal cropland) are being re-encoded and published.
