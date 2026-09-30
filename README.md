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
| TileRipper viewer | `js/tileripper/` | WebGL2 viewer: time scrub, looping playback up to 60 steps per second, click for values and a time-series chart, permalinks, WebM and GIF export |
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

Perceived scrub latency, input event to the frame showing the new timestep, 20 steps, same 36-cell 48-month store and browser before and after the decoder rewrite (worker-pool decode, time-window prefetch, no debounce, per-cell paint), 2026-09-30:

| Scenario | Steps shown, before | Steps shown, after | Lag median / p95, before | Lag median / p95, after | Frames over 33 ms, before / after |
|---|---:|---:|---:|---:|---:|
| Overview, cold, arrow keys | 4 of 20 | 20 of 20 | 263 / 527 ms | 3.2 / 15.8 ms | 16 / 0 |
| Overview, cold, slider drag | 2 of 20 | 20 of 20 | 334 / 693 ms | 5.3 / 16.6 ms | 0 / 0 |
| Overview, after 5 s idle, keys | 6 of 20 | 20 of 20 | 243 / 543 ms | 4.6 / 5.2 ms | 15 / 0 |
| 9 cells at LOD 0, cold, keys | 10 of 20 | 20 of 20 | 90 / 521 ms | 5.6 / 27.8 ms | 4 / 0 |
| 9 cells at LOD 0, after idle, keys | 20 of 20 | 20 of 20 | 8.3 / 8.8 ms | 2.7 / 3.7 ms | 0 / 0 |

Movie playback, two loops, holds counted separately at the wrap, 2026-09-30:

| Store, view, state | Requested | Achieved | Holds at wrap / elsewhere |
|---|---:|---:|---:|
| 36-cell local store, 9 cells, warm | 60 /s | 59.95 /s | 0 / 0 |
| 36-cell local store, 9 cells, cold | 60 /s | 56.4 /s | 0 / 9 |
| Ucayali over the internet, 4 cells, warm | 60 /s | 60.03 /s | 0 / 0 |
| Ucayali over the internet, 9 cells, cold | 60 /s | 6.0 /s | 0 / 155 |

A 9-cell overview of the 117-month store is about 11.7 MB per timestep, so a cold loop is bound by the link (about 55 MB/s here), and the 1 GiB decoded cache holds about 48 of the 117 timesteps at that size. A 4-cell view fits entirely and plays at the display rate.

Star-delta reconstruction runs in the fragment shader; the CPU loop it replaces cost 54 ms per 36-cell frame. Known limits: prefetch is greedy and will pull several hundred MB in the first seconds on any link; cold open of very small stores costs 30 to 40 ms for worker startup.

## Status

v0.1 draft. Layout is Zarr v3 groups per level, sharded by default so one file per spatial cell holds the whole time axis and a timestep is one range read. The public demo store is the Ucayali River near Santa María, Peru: 117 monthly Sentinel-2 composites from 2015 to 2026 over one of the fastest-migrating meandering reaches on Earth, served from an R2 bucket at data.tileripper.com. Sahara and Iowa remain the benchmark pair and can be re-encoded from the ingest example.
