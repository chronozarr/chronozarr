# chronozarr

Start with [Bring your own data](examples/bring_your_data/README.md): convert your rasters, read values in Python, and publish a self-hosted viewer and embed example.

Open a decade of analysis-ready satellite time series in a browser tab from a static bucket. Scrub it like video. Click for real numbers.

**chronozarr** is an open raster time-series format with a browser viewer at [chronozarr.org/demo](https://chronozarr.org/demo/): plain Zarr v3 with one group per pyramid level and one object per chunk, so a timestep of a spatial cell is one plain `GET`. Sharding, one file per cell for the time axis, is an option (`--shard`). The layout follows ndpyramid's `multiscales` attribute and the zarr `proj` and `spatial` conventions. An optional temporal profile, star-delta, stores most timesteps as residuals against a nearby anchor; the writer measures a sample of cells and enables it only when it shrinks the compressed bytes to 0.85 of the plain size or better. A store without it needs no chronozarr-aware reader.

Reading a store without chronozarr:

- xarray and other zarr-python 3 or zarrita clients open both layouts, unsharded (the default) and sharded (`tests/test_xarray_compat.py`). With chronozarr installed, `xr.open_dataset(path_or_url, engine="chronozarr")` also reconstructs star-delta timesteps lazily and returns float32 physical values (`stored * scale + offset`, NaN where invalid); `physical=False` returns the stored values.
- GDAL reads the layout through its Zarr driver. Stores without sharding (the default) open in GDAL 3.12 as a raster of time-major bands; sharded stores need GDAL 3.13 or newer, where the driver documents sharding support ([GDAL Zarr driver](https://gdal.org/en/stable/drivers/raster/zarr.html)). The writer emits the `_CRS` array attribute that GDAL reads, so GDAL assigns the CRS of an EPSG store. In a star-delta store GDAL sees residuals for non-anchor timesteps. `chronozarr export-cog STORE OUT_DIR` writes true-value Cloud Optimized GeoTIFFs for GDAL and QGIS from any store, including for GDAL versions that cannot read the store directly (extra `geo`).
- CarbonPlan zarr-layer reads the ndpyramid `multiscales` layout. A store without the temporal profile that was written after `pixels_per_tile` was dropped from `multiscales` (spec section 13) opens in zarr-layer unmodified (verified on the Ucayali store at level 1, point values equal to a direct Zarr read); a store written earlier opens with zarr-layer's `crs` and `bounds` constructor options; a star-delta store needs an adapter that reconstructs the residuals.

Spec: [spec/CHRONOZARR.md](https://github.com/chronozarr/chronozarr/blob/main/spec/CHRONOZARR.md) (v0.2, draft). Hosting: [docs/hosting.md](https://github.com/chronozarr/chronozarr/blob/main/docs/hosting.md). Growing a store: [docs/append.md](https://github.com/chronozarr/chronozarr/blob/main/docs/append.md). Embedding the viewer: [docs/embedding.md](https://github.com/chronozarr/chronozarr/blob/main/docs/embedding.md). Comparison with other formats: [docs/format-comparison.md](https://github.com/chronozarr/chronozarr/blob/main/docs/format-comparison.md).

## Who this is for

- **Researchers with stacks.** You have a Sentinel-2, Landsat or model time series in xarray, NetCDF or GeoTIFFs. `chronozarr encode` writes a store from an array in memory, `chronozarr convert` streams a COG manifest, a Zarr variable or a NetCDF file into one a timestep at a time, `xarray.open_zarr` or the `chronozarr` engine reads it back, and the viewer scrubs it.
- **Data publishers.** A store is one immutable prefix in a bucket with byte ranges and CORS, and nothing to run. `chronozarr doctor <url>` checks CORS, ranges, caching and decoding against the live URL. Recipes for S3 with CloudFront, R2, GCS and Source Cooperative are in [docs/hosting.md](https://github.com/chronozarr/chronozarr/blob/main/docs/hosting.md).
- **Map libraries integrating the decoder.** `js/chronozarr/` is a DOM-free reader on zarrita: `(lod, row, col, t)` to a typed-array cell, shard index cache, worker-pool decode and prefetch. A MapLibre custom layer built on it is in `js/maplibre/`.
- **Notebooks.** `chronozarr.open_store(path_or_url).to_xarray()` for arrays, and `chronozarr.view(store)` to look at a local store in the viewer from Jupyter (extra `notebook`).

## What is in this repo

| Part | Path | What it does |
|------|------|--------------|
| Format spec | `spec/CHRONOZARR.md` | Normative layout (one object per chunk by default, sharding optional), attributes, temporal encoding, pyramid, hosting rules |
| Python package `chronozarr` | `src/chronozarr/` | `encode()`, `open_store()`, `validate()`, `view()`; CLI `chronozarr encode / convert / append / validate / info / doctor / export-cog / stac`; xarray engine `chronozarr` |
| JS reader | `js/chronozarr/` | DOM-free reader on top of zarrita: cells by (lod, row, col, t), cache, prefetch |
| MapLibre layer | `js/maplibre/` | Custom layer that renders a store through the JS reader |
| chronozarr viewer | `js/demo/` | WebGL2 viewer: time scrub, looping playback up to 60 steps per second, click for values and a time-series chart, permalinks, WebM and GIF export; `?embed=1` compact mode with a postMessage API for host pages |
| Ingest example | `examples/sentinel2_pc/` | Monthly Sentinel-2 median composites from Planetary Computer |
| Water-mask example | `examples/water_masks/` | Derived NDWI and water-fraction stores with validity masks from the monthly mosaics ([docs/user-zero.md](https://github.com/chronozarr/chronozarr/blob/main/docs/user-zero.md)) |
| PNG frames example | `examples/png_frames/` | Georeferenced PNG frames converted into a store with no GeoTIFF step ([docs/png-frames.md](https://github.com/chronozarr/chronozarr/blob/main/docs/png-frames.md)) |
| Docs | `docs/` | Host recipes, appending, embedding, the format comparison, the PNG rules and the user-zero write-up |

## How it compares

- **PMTiles** packs tiles, images or vectors, for one moment. Its tile scheme is Web Mercator in practice and it has no native time axis; per-tile values are whatever the image encoding carries.
- **Mapbox raster-array** (MRT) is multi-band numeric tiles with a time series. Its decoder code is published in mapbox-gl-js (`src/data/mrt`), but the format is tied to Mapbox's tiling service and renderer.
- **CarbonPlan ndpyramid + zarr-layer** put Zarr pyramids in MapLibre with a time selector. zarr-layer supports arbitrary CRS through proj4 reprojection. Each timestep is its own chunk fetch, and stock zarr-layer needs an adapter to reconstruct star-delta residuals.

chronozarr keeps native projection and lossless values, serves a timestep as one plain `GET` of one chunk, and pre-stages a window of the time axis in the client so a timestep switch costs zero bytes on the wire once cached. The temporal encoding is the optional part: it was about 25% smaller on arid scenes, about 6% on vegetated ones, and 2.6% on the whole Ucayali demo store, so the writer decides per store. The layout is a trade the writer makes too: unsharded (the default) is one object per cell, level and timestep (about 5,900 for the 117-month imagery store), with no index reads, cheap CDN misses and appends that write only new objects; sharded (`--shard`) is 93 objects and one range read per timestep once the shard index is cached, with a CDN miss that costs time proportional to the shard size. [docs/format-comparison.md](https://github.com/chronozarr/chronozarr/blob/main/docs/format-comparison.md) has the row-by-row table, including when to choose each of the other tools.

## Install

`chronozarr` 0.2.1 is published on both [PyPI](https://pypi.org/project/chronozarr/0.2.1/) and [npm](https://www.npmjs.com/package/chronozarr/v/0.2.1). Both packages release from one `v*` tag, so their versions move together.

```bash
pip install chronozarr              # Python package and CLI; extras: geo (GeoTIFF input), notebook (view()), netcdf, dask
npm install chronozarr           # JavaScript reader and MapLibre layer
```

To work from a checkout of this repository:

```bash
uv sync                      # Python package and CLI; add --extra geo for GeoTIFF input, --extra notebook for view()
uv run chronozarr --help
```

`chronozarr` is the ES modules under `js/chronozarr/` and `js/maplibre/`, published as they are (no build step, no runtime dependency): `import { openStore } from 'chronozarr'` and `import { ChronozarrLayer } from 'chronozarr/maplibre'`. From a checkout, `cd js && npm install` installs only the test tooling. zarrita and its codecs are vendored under `js/vendor` (zarrita 0.7.5, @zarrita/storage 0.2.0, numcodecs 0.3.2, all MIT), so the viewer has no runtime third-party host; each vendored file header records its version, license and the SHA-256 of the published file, and the page loads no web font. The MapLibre demo page (`js/maplibre/index.html`) is the exception by design: it loads maplibre-gl from a pinned CDN version. Usage examples are in [js/README.md](https://github.com/chronozarr/chronozarr/blob/main/js/README.md).

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
js/demo/index.html?store=https://your-bucket/my_store
```

## Command line

`uv run chronozarr <command> --help` lists every option.

| Command | What it does |
|---------|--------------|
| `encode INPUT OUT` | Encode a Zarr store or NetCDF file with dims `(time, band, y, x)`, or a quoted glob of GeoTIFFs with the date in the file name, into a store. Options include `--encoding auto\|none\|star-delta`, `--codec`, `--level`, `--chunk-size`, `--shard/--no-shard` (default off), `--shard-time` (needs `--shard`), `--lods`. |
| `convert SOURCE OUT` | Convert a manifest (`.csv` with `uri,datetime[,bands]`, or `.json`) of COGs or georeferenced PNG frames (world file plus `--crs`, `.aux.xml`, or `--bounds` with `--crs`; RGBA alpha becomes the mask), a Zarr store or a NetCDF file into a store one timestep at a time, without loading the whole stack. Warps COGs that are off the target grid (`--crs`, `--transform`, `--shape`, `--resampling`), stages timesteps so `--resume` can continue an interrupted run, and `--dry-run` prints the size and time estimate only. Takes the encode options too (`--encoding`, `--codec`, `--chunk-size`, `--shard-time`, `--read-ahead`). |
| `append STORE INPUT` | Add timesteps at the end of an existing store: another store (for example one month written by `convert`), a Zarr store, a NetCDF file or a GeoTIFF glob. Writes only the objects that gain data and leaves every existing chunk byte-identical. The default unsharded layout appends by writing only new objects, where a sharded store rewrites its trailing shard; see [docs/append.md](https://github.com/chronozarr/chronozarr/blob/main/docs/append.md). |
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

A 9-cell overview of the 117-month store is about 11.7 MB per timestep, so a cold loop is bound by the link (about 55 MB/s here), and the cache, 1.5 GiB shared by the decoded and compressed tiers on an 8 GB machine, holds roughly half of the 117 timesteps at that size; idle prefetch stops after 64 MiB and expands only during playback. A 4-cell view fits entirely and plays at the display rate.

Coarse-first loading and the bandwidth-aware movie level, live Ucayali store with the link throttled to 50 Mbit/s and 40 ms, 2026-09-30, medians:

| Interaction | Before | After, first usable frame | After, full resolution |
|---|---:|---:|---:|
| Open | 2.03 s | 421 ms | 1.97 s |
| Big time jump | 6.00 s | 927 ms | 3.97 s |
| Zoom in | 4.86 s | under 2 ms (cached coarser cells) | 2.10 s |
| Playback at 10 steps/s requested | 3.4 /s | | 5.3 /s, level dropped by the link rule |

On an unthrottled link a frame expected within a second loads directly, so fast connections pay nothing for the staging. The 150 ms coarse-frame target holds on fast links (74 ms after metadata) and not at 50 Mbit/s, where three sequential requests per stage set a floor near 230 ms; shard_bytes hints remove one of them.

Whole frames and buffered playback, live Ucayali store, cold, device pixel ratio 2, 2026-10-01. A frame is one level and one timestep for every visible cell, shown complete or not at all: the target level when it is in memory, else the finest coarser level that is, else the frame already on screen. The whole loop is kept in memory at the coarsest useful level (level 3 here, about 80 MB) so a complete frame exists for every timestep. Playback buffers 2 s of frames before it starts and pauses on an indicator when it runs dry instead of holding frame by frame. "Partial" counts frames with some cells at another level or timestep; "whole" is the first frame covering the view at any level.

| Link | Tree | Open: whole / full | Scrub 20 steps: partial frames | Play 10 /s: achieved | Initial buffer | Buffering pauses | Holds |
|---|---|---:|---:|---:|---:|---:|---:|
| Natural | Before | 505 ms / 1.41 s | 8 of 9 | 5.7 /s | | | 98 (19.0 s) |
| Natural | After | 605 ms / 2.06 s | 0 of 37 | 10.0 /s | 1.9 s | 0 | 0 |
| 50 Mbit/s, 40 ms | Before | 545 ms / 2.25 s | 8 of 9 | 4.1 /s | | | 110 (35.3 s) |
| 50 Mbit/s, 40 ms | After | 499 ms / 2.58 s | 0 of 36 | 7.8 /s | 7.3 s | 2 (6.4 s) | 0 |

The cost is the full-resolution frame of a scrub step on a slow link, which lands later (median 3.7 to 4.5 s against 2.9 to 3.2 s at 50 Mbit/s) because a whole coarse frame of the right timestep is fetched first.

Appending one month to a 12-month Ucayali store (4 bands, 36 cells at level 0), against a re-encode of 32 s and 6.3 GB written, 2026-10-01 (full tables in [docs/append.md](https://github.com/chronozarr/chronozarr/blob/main/docs/append.md)):

| Layout | Append wall time | Bytes written per month | Rewritten |
|---|---:|---:|---|
| Unsharded (default) | 0.6 s | 55 MB | metadata only |
| Yearly time shards | 0.6 s | 55 to 660 MB, 4.4 GB over a 12-month cycle | the trailing shard, whole |
| Whole-axis shard (`--shard`) | 0.6 s | 55 MB, then 111 MB and growing | a second shard that grows every month |

The encoder default is now unsharded: a cold open of the published sharded store spent 6.5 of 6.8 s on the nine shard-index reads, because a CDN miss on a 2 KB range at the end of an 83 to 174 MB shard pulls the whole object, and an append to a sharded store rewrites the trailing shard. The cold-open and append numbers for the unsharded layout will be measured on the live store after the re-upload.

Cold open of the live unsharded store `chronozarr-4` from the deployed viewer with a cold edge cache, 2026-10-01: first whole frame 390 ms, complete frame at the target level 622 ms, against 6.8 s on the sharded store the same morning, where 6.5 s were the nine shard-index reads missing the edge cache. Publishing it took 274 s for 5,893 objects through the R2 S3 API (`scripts/r2_sync.py`), 150 s for the 17,088 objects of the water store. An append to a live store has not been exercised yet.

Star-delta reconstruction runs in the fragment shader; the CPU loop it replaces cost 54 ms per 36-cell frame. Known limits: the coarse loop pulls the whole time axis at its level after the first frame (tens of MB for a store a few cells wide); cold open of very small stores costs 30 to 40 ms for worker startup.

## Development checks

```bash
uv sync --extra dev --extra geo --extra netcdf --extra dask
uv run python scripts/check_architecture.py
uv run coverage run -m pytest -q -m unit
uv run coverage report
uv run coverage json
uv run coverage xml
npm ci --prefix js
npm run test:coverage --prefix js
npm run test:browser --prefix js
```

The architecture checker and `sentrux check .` share `.sentrux/rules.toml`: first-party
imports must be acyclic, shared writer and store modules must not depend on their callers,
and MapLibre/shared rendering must not depend on the demo. The dependency-free checker
runs in CI and includes deferred Python imports and static JavaScript imports/re-exports;
computed runtime imports are outside its scope.

Python coverage measures every package module and writes JSON/XML to
`data/reports/coverage/python/`. Native Node coverage measures only modules loaded by the
Node tests under `chronozarr`, `shared`, `maplibre`, and `demo`; it does not measure the
browser-only viewer or GPU execution. Browser tests verify those behaviors separately.
CI retains both coverage reports for 14 days.

For a before/after speed check on the same local fixture:

```bash
node scripts/audit_browser.mjs js data/spike/stress6x6 data/reports/browser-bench.json
```

The script uses headless Chromium with software WebGL, three cold runs and 20 switches.
Its additional `warmFullyLoaded` measurement primes all timesteps with a 2 GiB cache,
because the ordinary idle prefetch intentionally stops at 64 MiB. Inspect complete frames
and bytes alongside timings; local/loopback results do not measure CDN performance.

## Status

v0.2 draft (spec version `0.2.0`; every v0.1 store is a valid v0.2 store and readers accept both). The layout is Zarr v3 groups per level. The writer default is unsharded: one object per chunk, that is per cell, level and timestep (about 5,900 objects for the 117-month imagery store), so there is no shard index to read, a CDN miss costs one chunk and an append writes only new objects. Sharding stays available (`--shard`, `shard_time`) and every store written sharded stays valid: 93 objects and one range read per timestep once the shard index is cached, with a miss that costs time proportional to the shard size.

The public demo store is `ucayali_santa_maria/chronozarr-4`: the Ucayali River near Santa María, Peru, 117 monthly Sentinel-2 composites from 2015 to 2026, served from an R2 bucket at data.tileripper.com. The suffixes `-3` and `-4` are immutable revisions of this dataset's storage prefix, not chronozarr format or package versions. Both use spec v0.2.0: `chronozarr-3` was the historical sharded layout (6,451.9 MB in 93 files); the current `chronozarr-4` uses the unsharded default (6,451.8 MB in 5,893 files, values bit-identical). The writer's auto rule chose temporal encoding `none`: star-delta compressed to 0.988 of the plain size on the sampled cells, far short of the 0.85 needed to keep it. The same data with star-delta forced is 6,285.2 MB, so the plain store is 166.7 MB (about 2.7%) larger. That is the price of a store any Zarr v3 reader decodes without an adapter. The Measured tables above were taken on earlier stores of this reach and of the Sahara. Sahara and Iowa remain the benchmark pair and can be re-encoded from the ingest example. The legacy data hostname is retained for existing store URLs; the demo is at chronozarr.org/demo/.

A derived water store, `ucayali_santa_maria/water-2`, uses the same mosaics: NDWI as int16 with a scale of 1e-4, water as a scaled fraction, and a validity mask where no scene was observed, built by `examples/water_masks/`. It is the unsharded revision (1.73 GB in 17,088 files, values bit-identical to the historical sharded `water-1`, 201 files). It exercised a dtype other than uint16, explicit masks and physical units through the whole path. The current catalog lists the imagery store and the PNG frames demo (`png-1`); it does not list the water store. Stores can grow with `chronozarr append`, the viewer embeds in other pages with `?embed=1`, and georeferenced PNG frames convert directly.
