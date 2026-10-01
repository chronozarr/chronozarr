# TileRipper

Open a decade of analysis-ready satellite time series in a browser tab from a static bucket. Scrub it like video. Click for real numbers.

TileRipper is the viewer. **chronozarr** is the format underneath it: plain Zarr v3 with one group per pyramid level, sharded so one file holds a spatial cell's time axis and a timestep is one byte-range read. The layout follows ndpyramid's `multiscales` attribute and the zarr `proj` and `spatial` conventions. An optional temporal profile, star-delta, stores most timesteps as residuals against a nearby anchor; the writer measures a sample of cells and enables it only when it shrinks the compressed bytes to 0.85 of the plain size or better. A store without it needs no chronozarr-aware reader.

Reading a store without chronozarr:

- xarray and other zarr-python 3 or zarrita clients open the default sharded layout (`tests/test_xarray_compat.py`). With chronozarr installed, `xr.open_dataset(path_or_url, engine="chronozarr")` also reconstructs star-delta timesteps lazily and returns float32 physical values (`stored * scale + offset`, NaN where invalid); `physical=False` returns the stored values.
- GDAL reads the layout through its Zarr driver. Stores without sharding open in GDAL 3.12 as a raster of time-major bands; sharded stores need GDAL 3.13 or newer, where the driver documents sharding support ([GDAL Zarr driver](https://gdal.org/en/stable/drivers/raster/zarr.html)). The writer emits the `_CRS` array attribute that GDAL reads, so GDAL assigns the CRS of an EPSG store. In a star-delta store GDAL sees residuals for non-anchor timesteps. `chronozarr export-cog STORE OUT_DIR` writes true-value Cloud Optimized GeoTIFFs for GDAL and QGIS from any store, including for GDAL versions that cannot read the store directly (extra `geo`).
- CarbonPlan zarr-layer reads the ndpyramid `multiscales` layout. A store without the temporal profile is meant to open unchanged (not yet tested against zarr-layer in this repository); a star-delta store needs an adapter that reconstructs the residuals.

Spec: [spec/CHRONOZARR.md](https://github.com/jameshgrn/tile-ripper/blob/main/spec/CHRONOZARR.md) (v0.2, draft). Hosting: [docs/hosting.md](https://github.com/jameshgrn/tile-ripper/blob/main/docs/hosting.md). Comparison with other formats: [docs/format-comparison.md](https://github.com/jameshgrn/tile-ripper/blob/main/docs/format-comparison.md).

## Who this is for

- **Researchers with stacks.** You have a Sentinel-2, Landsat or model time series in xarray, NetCDF or GeoTIFFs. `chronozarr encode` writes a store from an array in memory, `chronozarr convert` streams a COG manifest, a Zarr variable or a NetCDF file into one a timestep at a time, `xarray.open_zarr` or the `chronozarr` engine reads it back, and the viewer scrubs it.
- **Data publishers.** A store is one immutable prefix in a bucket with byte ranges and CORS, and nothing to run. `chronozarr doctor <url>` checks CORS, ranges, caching and decoding against the live URL. Recipes for S3 with CloudFront, R2, GCS and Source Cooperative are in [docs/hosting.md](https://github.com/jameshgrn/tile-ripper/blob/main/docs/hosting.md).
- **Map libraries integrating the decoder.** `js/chronozarr/` is a DOM-free reader on zarrita: `(lod, row, col, t)` to a typed-array cell, shard index cache, worker-pool decode and prefetch. A MapLibre custom layer built on it is in `js/maplibre/`.
- **Notebooks.** `chronozarr.open_store(path_or_url).to_xarray()` for arrays, and `chronozarr.view(store)` to look at a local store in the viewer from Jupyter (extra `notebook`).

## What is in this repo

| Part | Path | What it does |
|------|------|--------------|
| Format spec | `spec/CHRONOZARR.md` | Normative layout, attributes, temporal encoding, pyramid, sharding, hosting rules |
| Python package `chronozarr` | `src/chronozarr/` | `encode()`, `open_store()`, `validate()`, `view()`; CLI `chronozarr encode / convert / validate / info / doctor / export-cog / stac`; xarray engine `chronozarr` |
| JS reader | `js/chronozarr/` | DOM-free reader on top of zarrita: cells by (lod, row, col, t), cache, prefetch |
| MapLibre layer | `js/maplibre/` | Custom layer that renders a store through the JS reader |
| TileRipper viewer | `js/tileripper/` | WebGL2 viewer: time scrub, looping playback up to 60 steps per second, click for values and a time-series chart, permalinks, WebM and GIF export |
| Ingest example | `examples/sentinel2_pc/` | Monthly Sentinel-2 median composites from Planetary Computer |
| Hosting and comparison | `docs/` | Host recipes and the format comparison |

## How it compares

- **PMTiles** packs tiles, images or vectors, for one moment. Its tile scheme is Web Mercator in practice and it has no native time axis; per-tile values are whatever the image encoding carries.
- **Mapbox raster-array** (MRT) is multi-band numeric tiles with a time series. Its decoder code is published in mapbox-gl-js (`src/data/mrt`), but the format is tied to Mapbox's tiling service and renderer.
- **CarbonPlan ndpyramid + zarr-layer** put Zarr pyramids in MapLibre with a time selector. zarr-layer supports arbitrary CRS through proj4 reprojection. Each timestep is its own chunk fetch, and stock zarr-layer needs an adapter to reconstruct star-delta residuals.

chronozarr keeps native projection and lossless values, serves a timestep as one range read from a sharded file, and pre-stages a window of the time axis in the client so a timestep switch costs zero bytes on the wire once cached. The temporal encoding is the optional part: it was about 25% smaller on arid scenes, about 6% on vegetated ones, and 2.6% on the whole Ucayali demo store, so the writer decides per store. [docs/format-comparison.md](https://github.com/jameshgrn/tile-ripper/blob/main/docs/format-comparison.md) has the row-by-row table, including when to choose each of the other tools.

## Install

Packages are not yet published. The intended names are `chronozarr` on PyPI and `chronozarr-js` on npm. Until then, work from a checkout of this repository:

```bash
uv sync                      # Python package and CLI; add --extra geo for GeoTIFF input, --extra notebook for view()
uv run chronozarr --help
```

The JavaScript reader is ES modules under `js/chronozarr/` and depends on `zarrita` 0.7.5 (`cd js && npm install`). The viewer has no runtime third-party host: zarrita and its codecs are vendored under `js/vendor` (zarrita 0.7.5, @zarrita/storage 0.2.0, numcodecs 0.3.2, all MIT), each file header records its version, license and the SHA-256 of the published file, and the page loads no web font. The MapLibre demo page (`js/maplibre/index.html`) is the exception by design: it loads maplibre-gl from a pinned CDN version.

## Quickstart

```python
import chronozarr

chronozarr.encode(da, "my_store", crs="EPSG:32631")   # da: (time, band, y, x) with x/y coordinates; the writer picks the temporal encoding
store = chronozarr.open_store("my_store")
store.read(t=42)                        # (band, y, x), exact stored values
store.to_xarray(lod=0)
```

```bash
uv run chronozarr encode scenes.nc my_store      # INPUT: Zarr, NetCDF, or a quoted GeoTIFF glob
uv run chronozarr validate my_store
uv run chronozarr doctor https://your-host/my_store
uv run chronozarr export-cog my_store cogs/           # true-value COGs for GDAL and QGIS (extra geo)
uv run chronozarr stac my_store --out catalog/        # static STAC Collection and Item (extra geo)
```

A plain reader needs no chronozarr at all (anchor timesteps are exact; in a star-delta store the other timesteps are residuals):

```python
import xarray as xr

ds = xr.open_zarr("my_store", group="0", zarr_format=3, chunks=None)
```

Serve the store from any static host that supports GET, byte ranges and CORS (S3, R2, GCS, Source Cooperative, a local range-capable server), then open the viewer:

```
js/tileripper/index.html?store=https://your-bucket/my_store
```

## Command line

`uv run chronozarr <command> --help` lists every option.

| Command | What it does |
|---------|--------------|
| `encode INPUT OUT` | Encode a Zarr store or NetCDF file with dims `(time, band, y, x)`, or a quoted glob of GeoTIFFs with the date in the file name, into a store. Options include `--encoding auto\|none\|star-delta`, `--codec`, `--level`, `--chunk-size`, `--shard-time`, `--no-shard`, `--lods`. |
| `convert SOURCE OUT` | Convert a COG manifest (`.csv` with `uri,datetime[,bands]`, or `.json`), a Zarr store or a NetCDF file into a store one timestep at a time, without loading the whole stack. Warps COGs that are off the target grid (`--crs`, `--transform`, `--shape`, `--resampling`), stages timesteps so `--resume` can continue an interrupted run, and `--dry-run` prints the size and time estimate only. Takes the encode options too (`--encoding`, `--codec`, `--chunk-size`, `--shard-time`, `--read-ahead`). |
| `validate STORE` | Check a store against the spec. Exit status 1 if it does not conform. |
| `info STORE` | Summarise a store: times, bands, temporal encoding and pyramid levels. |
| `doctor TARGET` | Diagnose an https URL or a local store path. A URL is probed as a browser would: root `zarr.json`, byte ranges, CORS, `HEAD` and caching headers. Both kinds then get the layout validated and one cell per level decoded and compared with a plain Zarr read. Exit status 1 only if a check fails; warnings and info lines are advice. `--origin` sets the `Origin` header. |
| `export-cog STORE OUT_DIR` | Write timesteps as true-value Cloud Optimized GeoTIFFs readable by GDAL and QGIS, one file per timestep. `--level` picks the pyramid level, `--times` picks timesteps (`all`, indices, slices, dates, date ranges). Needs extra `geo`. |
| `stac STORE --out DIR` | Write a static STAC Collection and Item for a store: the Zarr asset, extent, band metadata, the datacube extension and the recorded provenance. `--href` sets the public store location. Needs extra `geo`. |

`examples/sentinel2_pc/ingest.py` builds a Sentinel-2 store from Planetary Computer with band metadata, a 0/1 coverage plane and provenance recorded in it; `--stac` also writes a static STAC Collection and Item.

## Measured

Browser decode of one real Sentinel-2 chunk (4 x 512 x 512 uint16, 2 MB raw), median of 15, 2026-09-29:

| Codec | Bytes | Decode |
|-------|------:|-------:|
| zstd via zarrita (WASM) | 1,284,781 | 4.5 ms |
| gzip via native DecompressionStream | 1,387,409 | 6.7 ms |
| zstd via fzstd (pure JS) | 1,284,781 | 14.1 ms |

Four real Ucayali LOD 0 chunks (2,097,152 bytes each), zarrita 0.7.5 with vendored codec modules served locally, headless Chrome, median of 20, 2026-09-30, bit-exact in every case:

| Codec | Bytes per chunk | Decode, steady state | Decode inside a worker |
|-------|------:|-------:|-------:|
| zstd level 5 (writer default) | 1,376,469 | 4.4 ms | 4.4 ms |
| blosc, zstd level 1, byte shuffle | 1,520,154 (+10.4%) | 3.5 ms | 3.3 ms |

Byte shuffle did not shrink the zstd stream on this data. The 10.4% size penalty alone keeps zstd level 5 as the default; blosc is permitted.

Temporal encoding, compressed bytes of star-delta against plain storage with the same codec: about 25% smaller on arid scenes, about 6% smaller on vegetated ones, and 2.6% smaller (6,285.2 MB against 6,451.9 MB) on the full Ucayali demo store. The writer samples level 0 cells and keeps star-delta only at 0.85 of the plain size or better.

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

Coarse-first loading and the bandwidth-aware movie level, live Ucayali store with the link throttled to 50 Mbit/s and 40 ms, 2026-09-30, medians:

| Interaction | Before | After, first usable frame | After, full resolution |
|---|---:|---:|---:|
| Open | 2.03 s | 421 ms | 1.97 s |
| Big time jump | 6.00 s | 927 ms | 3.97 s |
| Zoom in | 4.86 s | under 2 ms (cached coarser cells) | 2.10 s |
| Playback at 10 steps/s requested | 3.4 /s | | 5.3 /s, level dropped by the link rule |

On an unthrottled link a frame expected within a second loads directly, so fast connections pay nothing for the staging. The 150 ms coarse-frame target holds on fast links (74 ms after metadata) and not at 50 Mbit/s, where three sequential requests per stage set a floor near 230 ms; shard_bytes hints remove one of them.

Star-delta reconstruction runs in the fragment shader; the CPU loop it replaces cost 54 ms per 36-cell frame. Known limits: prefetch is greedy and will pull several hundred MB in the first seconds on any link; cold open of very small stores costs 30 to 40 ms for worker startup.

## Status

v0.2 draft (spec version `0.2.0`; every v0.1 store is a valid v0.2 store and readers accept both). The layout is Zarr v3 groups per level, sharded by default so one file per spatial cell and time shard holds the time axis and a timestep is one range read.

The public demo store is `ucayali_santa_maria/chronozarr-3`: the Ucayali River near Santa María, Peru, 117 monthly Sentinel-2 composites from 2015 to 2026 over one of the fastest-migrating meandering reaches on Earth, served from an R2 bucket at data.tileripper.com. It is a v0.2 store. The writer's auto rule chose temporal encoding `none`: star-delta compressed to 0.988 of the plain size on the sampled cells, far short of the 0.85 needed to keep it. The store is 6,451.9 MB in 93 files; the same data with star-delta forced is 6,285.2 MB, so the plain store is 166.7 MB (about 2.7%) larger. That is the price of a store any Zarr v3 reader decodes without an adapter. The Measured tables above were taken on earlier stores of this reach and of the Sahara. Sahara and Iowa remain the benchmark pair and can be re-encoded from the ingest example.
