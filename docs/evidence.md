# Evidence

This file holds measurements, tested versions, dates and caveats. User docs link here. Each section covers one topic.

- [Reader checks](#reader-checks)
- [Layout choice](#layout-choice)
- [Codec decode](#codec-decode)
- [v0.3 against v0.2](#v03-against-v02)
- [Viewer measurements](#viewer-measurements)
- [Appending](#appending)
- [PNG frames](#png-frames)
- [Embedding](#embedding)
- [MapLibre layer](#maplibre-layer)
- [JavaScript dependencies](#javascript-dependencies)
- [Comparison notes](#comparison-notes)
- [Live store and publishing](#live-store-and-publishing)
- [Hosting observations](#hosting-observations)

## Reader checks

GDAL 3.13.3 read the two-level v0.3 fixture. It gave correct EPSG:32618, transforms and all six oracle values, without warnings. It did so both with and without `_CRS`. Selected raster slices did not expose automatic overviews.

CarbonPlan zarr-layer 0.10.0 with zarrita 0.7.5 rendered the fixture. It needed no CRS, bounds or spatial-dimension overrides. It returned 1107 at the checked pixel centre.

Separate-mask handling and framebuffer color calibration were not established.

xarray needs an explicit level group. `xarray.open_zarr(store, group="0")` opens a selected level with ordinary stored values. `open_zarr` on the store root may return an empty dataset.

GDAL 3.12.4 needs `_CRS` to assign the CRS. GDAL 3.13.3 assigns the CRS without that alias. It exposes the tested pyramid as subdatasets and does not attach the levels as overviews. Subdataset enumeration does not show that GDAL attaches overview levels or applies a separate mask.

zarr-layer returned the known value of the true-value fixture at the correct map location.

These checks cover reading only. Append, performance and cross-origin hosting were not checked for these readers.

## Layout choice

The writer default is unsharded: one object per chunk. A chunk is one cell, one level and one timestep. The 117-month imagery store has about 5,900 objects. There is no shard index to read. A CDN miss costs one chunk. An append writes only new objects.

A sharded store (`--shard`, `shard_time`) has 93 objects for the same data. A reader needs one range read per timestep once the shard index is cached. A CDN miss costs time proportional to the shard size.

Every store written sharded stays valid.

Why the default changed: a cold open of the published sharded store spent 6.5 of 6.8 s on the nine shard-index reads. A CDN miss on a 2 KB range at the end of an 83 to 174 MB shard pulls the whole object. An append to a sharded store rewrites the trailing shard.


Cloudflare documents a 512 MB cacheable object limit on the Free, Pro and Business plans. Check the current figure. A shard is about `n_time` times the compressed size of one cell-timestep. The sharded Ucayali store has 117 timesteps and shards up to 174 MB.

A miss on a range read of an uncached object pulled the whole object from R2. The 1,876-byte shard-index read at the end of an 83 to 174 MB shard took 2 to 11 s. It took 0.36 s on a 41 MB shard. Nine of them were 6.5 of a 6.8 s cold open.

The largest object of the unsharded Ucayali store is 1.8 MB.

Cold open of the live unsharded store `chronozarr-4` from the deployed viewer, cold edge cache, 2026-10-01: first whole frame 390 ms. Complete frame at the target level 622 ms. The sharded store the same morning took 6.8 s, and 6.5 s of that were the nine shard-index reads missing the edge cache.

## Codec decode

Browser decode of one real Sentinel-2 chunk (4 x 512 x 512 uint16, 2 MB raw), median of 15, 2026-09-29:

| Codec | Bytes | Decode |
|-------|------:|-------:|
| zstd via zarrita (WASM) | 1,284,781 | 4.5 ms |
| gzip via native DecompressionStream | 1,387,409 | 6.7 ms |
| zstd via fzstd (pure JS) | 1,284,781 | 14.1 ms |

Four real Ucayali LOD 0 chunks (2,097,152 bytes each). zarrita 0.7.5 with vendored codec modules served locally, headless Chrome, median of 20, 2026-09-30. Bit-exact in every case:

| Codec | Bytes per chunk | Decode, steady state | Decode inside a worker |
|-------|------:|-------:|-------:|
| zstd level 5 (writer default) | 1,376,469 | 4.4 ms | 4.4 ms |
| blosc, zstd level 1, byte shuffle | 1,520,154 (+10.4%) | 3.5 ms | 3.3 ms |

Byte shuffle did not shrink the zstd stream on this data. The 10.4% size penalty alone keeps zstd level 5 as the default. Blosc is permitted.

## v0.3 against v0.2

Five interleaved A/B rounds on unchanged readers. Localhost, headless Chrome 154, 1280 x 900, DPR 1, Ucayali LOD 0, 36 cells, 2026-10-03. Source: `data/spike/js_v03_bench.json`, `hypothesis_1.unchanged` (local, gitignored). Medians of the five round medians:

| Measurement | v0.2 | v0.3 |
|---|---:|---:|
| Cold complete frame | 87.4 ms | 91.1 ms |
| Warm stepping | 4.9 ms | 4.5 ms |
| Decode per 2 MiB chunk | 4.4 ms | 4.4 ms |
| Cold requests | 37 | 37 |
| Cold response-body bytes | 39,084,594 | 39,088,419 |

v0.3 equals v0.2 within noise. The earlier non-interleaved timings are superseded.

Some warm stepping and jumping samples are incomplete. Their medians do not establish a complete-frame gate pass. Zero sampled wire bytes do not establish zero traffic over the entire phase. No tuning was adopted.

## Viewer measurements

The tables in this section were measured on earlier format versions. They are kept as dated measurements.

### Cold open and warm switch

Viewer on the v0.1 Sahara store (128 months, 4 bands, 6 x 6 cells at LOD 0, 3.9 GB in 93 files). Localhost, HTTP cache bypassed, 2026-09-29:

| Measurement | Real-GPU Chromium, 36-cell 12-month stress store | Built-in browser pane, full 128-month Sahara store |
|-------------|------:|------:|
| Cold open to first complete frame | 257 ms, 109 requests, 43.8 MB | 475 ms median, 109 requests, 35.2 MB |
| Cold open without consolidated metadata | 256 ms | 556 to 1478 ms |
| Warm timestep switch, stepping (median / p95) | 3.5 / 15 ms | 6.3 / 19 ms |
| Warm switch wire bytes and requests | 0 / 0 | 0 / 0 |
| Product switch | about 1 ms | under 1 ms |
| Decode per 2 MB chunk | 4.6 ms | 10 ms under load (4.5 ms idle) |

### Scrub latency

Perceived scrub latency is the time from the input event to the frame that shows the new timestep. 20 steps, same 36-cell 48-month store and browser before and after the decoder rewrite. The rewrite added worker-pool decode, time-window prefetch, no debounce and per-cell paint. 2026-09-30:

| Scenario | Steps shown, before | Steps shown, after | Lag median / p95, before | Lag median / p95, after | Frames over 33 ms, before / after |
|---|---:|---:|---:|---:|---:|
| Overview, cold, arrow keys | 4 of 20 | 20 of 20 | 263 / 527 ms | 3.2 / 15.8 ms | 16 / 0 |
| Overview, cold, slider drag | 2 of 20 | 20 of 20 | 334 / 693 ms | 5.3 / 16.6 ms | 0 / 0 |
| Overview, after 5 s idle, keys | 6 of 20 | 20 of 20 | 243 / 543 ms | 4.6 / 5.2 ms | 15 / 0 |
| 9 cells at LOD 0, cold, keys | 10 of 20 | 20 of 20 | 90 / 521 ms | 5.6 / 27.8 ms | 4 / 0 |
| 9 cells at LOD 0, after idle, keys | 20 of 20 | 20 of 20 | 8.3 / 8.8 ms | 2.7 / 3.7 ms | 0 / 0 |

### Playback

Movie playback, two loops. Holds are counted separately at the wrap. 2026-09-30:

| Store, view, state | Requested | Achieved | Holds at wrap / elsewhere |
|---|---:|---:|---:|
| 36-cell local store, 9 cells, warm | 60 /s | 59.95 /s | 0 / 0 |
| 36-cell local store, 9 cells, cold | 60 /s | 56.4 /s | 0 / 9 |
| Ucayali over the internet, 4 cells, warm | 60 /s | 60.03 /s | 0 / 0 |
| Ucayali over the internet, 9 cells, cold | 60 /s | 6.0 /s | 0 / 155 |

A 9-cell overview of the 117-month store is about 11.7 MB per timestep. A cold loop is bound by the link (about 55 MB/s here).

The cache holds roughly half of the 117 timesteps at that size. The cache is 1.5 GiB, shared by the decoded and compressed tiers, on an 8 GB machine. Idle prefetch stops after 64 MiB and expands only during playback. A 4-cell view fits entirely and plays at the display rate.

### Coarse-first loading

Coarse-first loading and the bandwidth-aware movie level. Live Ucayali store, link throttled to 50 Mbit/s and 40 ms, 2026-09-30, medians:

| Interaction | Before | After, first usable frame | After, full resolution |
|---|---:|---:|---:|
| Open | 2.03 s | 421 ms | 1.97 s |
| Big time jump | 6.00 s | 927 ms | 3.97 s |
| Zoom in | 4.86 s | under 2 ms (cached coarser cells) | 2.10 s |
| Playback at 10 steps/s requested | 3.4 /s | | 5.3 /s, level dropped by the link rule |

On an unthrottled link, a frame expected within a second loads directly. Fast connections pay nothing for the staging.

The 150 ms coarse-frame target holds on fast links (74 ms after metadata). It does not hold at 50 Mbit/s. Three sequential requests per stage set a floor near 230 ms there. `shard_bytes` hints remove one of the three.

### Whole frames and buffered playback

Live Ucayali store, cold, device pixel ratio 2, 2026-10-01.

A frame is one level and one timestep for every visible cell. The viewer shows a frame complete or not at all. It uses the target level when that level is in memory. Otherwise it uses the finest coarser level that is in memory. Otherwise it keeps the frame already on screen.

The viewer keeps the whole loop in memory at the coarsest useful level (level 3 here, about 80 MB). A complete frame then exists for every timestep. Playback buffers 2 s of frames before it starts. It pauses on an indicator when the buffer runs dry, instead of holding frame by frame.

"Partial" counts frames with some cells at another level or timestep. "Whole" is the first frame that covers the view at any level.

| Link | Tree | Open: whole / full | Scrub 20 steps: partial frames | Play 10 /s: achieved | Initial buffer | Buffering pauses | Holds |
|---|---|---:|---:|---:|---:|---:|---:|
| Natural | Before | 505 ms / 1.41 s | 8 of 9 | 5.7 /s | | | 98 (19.0 s) |
| Natural | After | 605 ms / 2.06 s | 0 of 37 | 10.0 /s | 1.9 s | 0 | 0 |
| 50 Mbit/s, 40 ms | Before | 545 ms / 2.25 s | 8 of 9 | 4.1 /s | | | 110 (35.3 s) |
| 50 Mbit/s, 40 ms | After | 499 ms / 2.58 s | 0 of 36 | 7.8 /s | 7.3 s | 2 (6.4 s) | 0 |

The cost is the full-resolution frame of a scrub step on a slow link. It lands later: median 3.7 to 4.5 s against 2.9 to 3.2 s at 50 Mbit/s. The viewer fetches a whole coarse frame of the right timestep first.

### Known limits

- The coarse loop pulls the whole time axis at its level after the first frame. That is tens of MB for a store a few cells wide.
- Cold open of very small stores costs 30 to 40 ms for worker startup.

## Appending

Appending one month to a 12-month Ucayali store (4 bands, 36 cells at level 0). A re-encode of the same store took 32 s and wrote 6.3 GB. 2026-10-01.

| Layout | Append wall time | Bytes written per month | Rewritten |
|---|---:|---:|---|
| Unsharded (default) | 0.6 s | 55 MB | metadata only |
| Yearly time shards | 0.6 s | 55 to 660 MB, 4.4 GB over a 12-month cycle | the trailing shard, whole |
| Whole-axis shard (`--shard`) | 0.6 s | 55 MB, then 111 MB and growing | a second shard that grows every month |

These are v0.2 measurements. They were not repeated for v0.3.

Cost model for a sharded store:

- A sharded store rewrites the shard that receives each new timestep, whole.
- The average rewrite is `(shard_time + 1) / 2` chunks per cell per append.
- Over a 12-month cycle at `shard_time` 12, the writes are 4389 MB against 686 MB.
- A whole-axis shard (one shard for the initial axis) opens a second shard as long as the first. It rewrites that shard on every append, up to 117 chunks per cell for a 117-month store.
- The first batch of a sharded store may be a single timestep at any `--shard-time`.

An unsharded append uploads only the objects it wrote. The measurement had 63 objects.

An append to a live store has not been exercised yet.

Advice that follows from the numbers:

- Choose a finite `--shard-time` (12 for monthly data) only when object count matters more than the rewrite cost.
- Choose the whole-axis `--shard-time` for archives that are not appended to.

## PNG frames

### Example run

`examples/png_frames/` was run on the Ucayali mosaics when it was added, on 2026-10-01 (commit `a2b557d`). The machine was an M3 Max laptop with other work running. The writer was v0.2, and the store was `png-1`.

The 36 frames cover 2019-01 to 2021-12. Each is a 1024 x 1024 pixel window at row 768, column 1280 of the 2765 x 2759 mosaic. The stretch maps DN 185 to 1758 to 0 to 255. The masked share of a frame runs from 0 to 76.8 %. Six of the 36 frames are over 3 % masked.

| Quantity | Value |
|---|---|
| Frames on disk | 74.7 MB for 36 PNGs, with GDAL's default PNG compression |
| One PNG | 2.08 MB, against 4.19 MB of raw RGBA |
| Raw in the store | 113.2 MB of bands and 37.7 MB of mask |
| Store | 112.9 MB in 35 files |
| Level 0 | 89.6 MB, with its masks |
| Level 1 | 23.2 MB, with its masks |
| Masks | 0.23 MB |
| `convert` | read 0.5 s, encode 1.0 s, 1.5 s in all |
| Read of one 1024 x 1024 frame | about 0.03 s |

The store is 1.5 times the size of the PNGs. Level 1 is a quarter of level 0. PNG row filters compress rendered imagery better than zstd on the raw bytes: level 0 alone is 1.2 times the PNGs.

The 35 files are a sharded layout. The writer default has been unsharded since 2026-10-01, so a rerun writes more objects.

### Checks on the example

- `chronozarr validate` printed "conforms to chronozarr 0.2.0". `chronozarr doctor` on the local path gave 3 ok, 0 info, 0 warnings and 0 failures.
- `check_store.py` found, for all 36 frames, that the red, green, blue and mask of the store equal the red, green, blue and alpha != 0 of the PNG, bit for bit.
- The same frames without world files, converted with `--crs EPSG:32718 --bounds 498450,9151960,508690,9162200`, gave a store identical to `png-1` in data, mask and transform. So did the frames with an `.aux.xml` each and no `--crs`.
- In the headless viewer with software WebGL, True color was the active product and the only color product enabled. False color, NDVI, NDWI and Water were disabled.
- A masked pixel was drawn as the background color (9, 12, 18). The valid pixel six pixels to its right was drawn as the stored color.
- A click read the stored `red`, `green` and `blue` values (200, 238, 184 at pixel 154, 43 of 2019-10), which is what the PNG holds there. Playback buffered, stepped through six timesteps and stopped. There were no console errors.
- The screenshots were `data/reports/viewer_ucayali_png_truecolor.png` (2019-10, 38 % masked) and `data/reports/viewer_ucayali_png_edge.png` (the masked edge at 8x with the inspector open).

### Sidecar and conversion checks

On 2026-10-09 three synthetic frames of 48 x 64 pixels in EPSG:32718 with 10 m pixels were converted with the commands of the guide. The three setups were a world file with `--crs`, an `.aux.xml` with no options, and no sidecar with `--crs` and `--bounds`. Each store passed `chronozarr validate`, which printed "conforms to chronozarr 0.3.0".

Other results from 2026-10-09, with GDAL 3.10.3 through rasterio 1.4.4:

- A frame with a world file and an `.aux.xml` read its transform from the world file and its CRS from the `.aux.xml`.
- GDAL wrote the world file of a PNG with the extension `.wld`. It held 498455 and 9162195 for a frame with its upper-left corner at 498450, 9162200 and 10 m pixels. That is the center of the upper-left pixel.
- A palette PNG gave the error text shown in the guide.
- A gray PNG became a store with one band named `1`. An RGBA PNG became three bands named `red`, `green` and `blue`, plus a mask.
- Frames of different sizes with `--bounds` failed and named both frames.

World files and `.aux.xml` files next to `http(s)` PNGs were checked against a local range server, not a CDN. The date is not recorded.

## Embedding

Checked on 2026-10-09 against `https://chronozarr.org`:

- `GET /demo/` returned 200 with no `X-Frame-Options` header and no `Content-Security-Policy` header.
- `GET /demo?embed=1&store=x` returned 301 to `/demo/?embed=1&store=x`.
- `GET /examples/embed.html` returned 307 to `/examples/embed`, which returned 200.

Also on 2026-10-09, a host page on `http://127.0.0.1:8791` drove the viewer on `http://127.0.0.1:8765` with the live store `ucayali_santa_maria_v03`:

- A `set` with `range: [0, 0.3]` changed `state.range`, and `range: null` reset it to `null`.
- A `set` with `range: [2, 1]` returned `bad_set`.
- A message with `v: 2` and a message with the type `chronozarr:nope` returned `bad_message`.
- A `set` with `t: 5000` returned `bad_set`.

The sample messages in the guide are values from that run. The live store has 117 timesteps, and they are not consecutive months: the second timestep is 2016-04-01. The first click was at pixel (1155, 1394), at level 0.

The browser test `js/e2e/viewer-embed.spec.js` covers the 320 px layout. Sandbox settings other than `allow-scripts allow-same-origin` are untested.

## MapLibre layer

### Placement checks

The layer and `js/maplibre/verify/` were added on 2026-09-30 (commit `bdc5938`). The results below are for the Ucayali store in Chromium on an M-series Mac at 1280 x 800, with bearing 0 unless noted. They carry no date.

| Check | Result |
|---|---|
| Footprint outline at zoom 9.5 (level 3), 11 (level 2), 12.5 (level 0) and 15.5 (level 0), and at bearing 30 with pitch 45, and at bearing -50 with pitch 40 | 0 to 8 pixels per view were drawn outside the pyproj outline, and none was more than 0.001 px beyond it |
| Unlit pixels within 8 px inside the outline, about 400 sampled evenly per view | every one was a nodata gap at level 0 |
| Interior texel boundaries, levels 0 to 3, at about 4 px per texel | 60 to 84 boundaries per level, wherever neighbouring texels differ |
| Offset of those boundaries from pyproj | all within 0.496 px, where 0.5 px is the limit of pixel-center sampling, with a mean offset below 0.03 px and no stray boundaries |
| Colors against `js/demo/viewer.js`, same store, timestep and texel | 20 of 20 texel and product pairs identical in 8 bits (NDVI, NDWI, water, true color and false color) |

### Error bounds

`js/test/maplibre-mesh.test.js` asserts two bounds. A mesh of 8 x 8 quads over a 512 px cell of a 10 m UTM store stays within 0.01 px at zoom 22, from interpolation plus float32 rounding. The error falls as the divisions grow. Cells that share an edge share their edge vertices to 1e-11 of the world, so the raster has no seams.

### Loading order and prefetch

These numbers carry no date. The first view fetches the coarsest level first. On a 3 MB/s link with 120 ms latency, for 4 cells at level 0 of the Ucayali store (12 MB), the first pixels appeared after 2.5 s instead of 5.5 s. The full-detail view was ready after 7.6 s instead of 5.8 s. On a fast link neither differed.

With `prefetch: true` the demo moved 260 MB in 40 s for a 4-cell view of the Ucayali store.

## JavaScript dependencies

### MapLibre GL JS

The MapLibre layer was tested with MapLibre GL JS 6.10.0 only. The npm registry lists 6.10.0 as published on 2026-09-15. `js/maplibre/index.html` loads that version from cdn.jsdelivr.net. On 2026-10-09 the latest release was 6.13.0, which was not tested. The layer needs MapLibre 5 or later, which passes `defaultProjectionData.mainMatrix`.

### CDN headers

On 2026-10-09 jsDelivr and unpkg both returned `access-control-allow-origin: *` for `chronozarr@0.3.1/chronozarr/decoder.js`.

### Vendored packages

The headers of the files in `js/vendor/` record these versions: zarrita 0.7.5, @zarrita/storage 0.2.0, numcodecs 0.3.2 and gifenc 1.0.3. Each header also holds the SHA-256 of the published file, except the gifenc header, which holds its license text.

## Comparison notes

The statements about other tools in [format-comparison.md](format-comparison.md) were read from their documentation and source on 2026-09-30. Two were checked again on 2026-10-09. The `mapbox-gl-js` repository has the directory `src/data/mrt`. The README of zarr-layer states arbitrary CRS support through proj4 and lists the WGS84 UTM zones. The other statements were not checked again.

## Live store and publishing

The demo catalog lists two v0.3 stores on `data.chronozarr.org`, in the R2 bucket `chronozarr-stores`:

- `ucayali_santa_maria_v03`: 117 monthly Sentinel-2 composites, unsharded, 5,893 objects, 6,451,772,327 bytes.
- `ucayali_santa_maria/png-v03`: the PNG frames demo, sharded, 35 objects, 112,879,724 bytes.

The R2 bucket uses `deploy/r2-cors.json` and the Cache Rule in [hosting.md](hosting.md#32-cloudflare-r2).

History of the imagery store:

| Date | Event |
|---|---|
| Before 2026-10-01 | `ucayali_santa_maria/chronozarr-3`, sharded, 93 objects, published on the previous data host. |
| 2026-10-01 | `ucayali_santa_maria/chronozarr-4`, unsharded, 5,893 objects, replaced it. The values are bit-identical. |
| 2026-10-03 | `ucayali_santa_maria_v03` replaced `chronozarr-4` in the catalog, converted with `chronozarr convert`. |
| 2026-10-03 | Both v0.3 stores were copied to the bucket `chronozarr-stores` and served from `data.chronozarr.org`. All 5,928 objects matched by ETag and size. |
| 2026-10-05 | The previous bucket was deleted, with the v0.2 stores and the v0.2 water store `ucayali_santa_maria/water-2`. |

The suffixes `-3` and `-4` count dataset revisions. Both stores used spec v0.2.0. v0.3 readers reject v0.2 stores; convert them with `chronozarr convert`.

Doctor results:

| Store | Date | Result |
|---|---|---|
| `ucayali_santa_maria/chronozarr-2` | 2026-09-30 | 15 ok, 2 info, 0 warnings. `edge cache` HIT, `timing-allow-origin` `*`, `cache-control` `max-age=31536000`. |
| `ucayali_santa_maria/chronozarr-4` | not recorded | 13 ok, 0 failures, at upload. |
| `ucayali_santa_maria_v03` | 2026-10-03 | 13 ok, 3 info, 0 warnings. Decode matched at levels 0 to 3. |
| `ucayali_santa_maria/png-v03` | 2026-10-03 | 12 ok, 2 info, 0 warnings. Decode matched at levels 0 and 1. |

The info lines on 2026-10-03 reported `edge cache` DYNAMIC and no `Timing-Allow-Origin`. The Cache Rule was added on 2026-10-05.

Publishing times through the R2 S3 API with `scripts/r2_sync.py`:

- The 5,893 objects of `chronozarr-4` took 274 s.
- The 17,088 objects of the water store took 150 s.

Publishing through wrangler starts in about 2 seconds per object. At 4 parallel uploads, the 5,893-object store takes about 50 minutes. The 17,088-object water store takes 2.4 hours. A sharded store of around 100 objects takes a minute or two.

## Hosting observations

### Doctor

The `doctor` checklist in [hosting.md](hosting.md#1-checklist) was read from `src/chronozarr/doctor.py` on 2026-10-01. If that file changes, the file is the authority.

### Cache copies per requesting site

R2 adds `Vary: Origin` to responses on a bucket with a CORS policy, and Cloudflare keeps a separate cached copy for each `Origin`. Checked on `data.chronozarr.org` on 2026-10-08 with one chunk (`ucayali_santa_maria_v03/2/data/c/46/0/1/1`):

| Request | First | Second |
|---|---|---|
| `Origin: https://site-a.example` | MISS | HIT |
| `Origin: https://site-b.example` | MISS | HIT |
| No `Origin` header | MISS | HIT |
| `Origin: https://chronozarr.org` | MISS | |

A Transform Rule that removed `Vary` from responses did not change this: on 2026-10-08 a second origin still missed after the first had cached the chunk. The rule was deleted, because without `Vary` a browser could reuse a response that has no CORS header.

### Cloudflare cache rule

Checked on `data.chronozarr.org` on 2026-10-05, with the rule from hosting section 3.2 (Edge TTL follows `Cache-Control`; zone Browser Cache TTL respects existing headers):

| Object | First request | Second request | `Cache-Control` sent |
|---|---|---|---|
| `ucayali_santa_maria_v03/0/data/c/0/0/0/0` | MISS | HIT | `public, max-age=31536000, immutable` |
| `ucayali_santa_maria_v03/zarr.json` | MISS | HIT | `public, max-age=300` |
| A `bytes=0-1023` range on a chunk | MISS, `206` | HIT, `206` | `public, max-age=31536000, immutable` |

Before the zone Browser Cache TTL was changed, the same `zarr.json` reached clients with `max-age=14400`. The zone default of four hours had replaced the 300-second origin value.

Before 2026-10-03 the data host used a rule that overrode both TTLs with one year. Verified on 2026-09-30: `zarr.json` returned MISS then HIT with an `age` header, and a `206` range request on a shard returned MISS then HIT. That rule hid appends, and section 3.2 no longer recommends it.

A Cache Rule whose expression sits in a URI wildcard value matches nothing. R2 responses then stay `DYNAMIC` with no `Timing-Allow-Origin`. For an R2 hostname, the dashboard warns that the rule "may not apply to your traffic". The rule applies regardless.

### Source Cooperative

Observed on one public object on 2026-09-30 (`kerner-lab/fields-of-the-world`):

- A bounded range returns `206` with `Content-Range` and `Accept-Ranges: bytes`.
- The response has `Access-Control-Allow-Origin: *`, `Access-Control-Allow-Headers: *` and `Access-Control-Expose-Headers: *`.
- `OPTIONS` returns `204`.
- There is no `Cache-Control` and no `Timing-Allow-Origin`.
- `cf-cache-status` is `DYNAMIC`.

Expected `chronozarr doctor` result: checks 1 to 8 and 12 pass. `cache-control` warns for a versioned prefix. `edge cache` and `timing-allow-origin` are info.

The proxy is documented as beta. The storage behind it (S3, GCS, Azure, R2) is not the user's choice.

Whether the proxy stores and serves the `--cache-control` value is untested.

### Timing-Allow-Origin

Without `Timing-Allow-Origin`, `PerformanceResourceTiming.transferSize` is 0 for cross-origin requests. In-page benchmarks then under-report bytes. The viewer counts bytes only from `Content-Length` in that case.

### Preflight

A browser sends a preflight only for a suffix range. Browsers do not preflight a bounded `bytes=a-b`.

A store written with `shard_bytes` lets a reader fetch every shard index as a bounded range. Checks 6 and 7 then cost nothing in practice, but doctor still reports them.

An unsharded store is read with plain `GET`s and no `Range` header.

### Cache lifetime after an append

The trailing shard of a cell holds its last timestep. When the next shard opens, the old one becomes immutable. It keeps the `max-age=300` it was uploaded with, because `--newer-than` does not touch it. That costs an origin revalidation every five minutes per shard.

### Open viewers after an append

A viewer keeps the root `zarr.json` it loaded until the page reloads. It shows the old timesteps. Everything it already reads stays correct: chunks, offsets and references of existing timesteps do not change.

For a sharded store, one failure exists. The viewer fetches a shard index after an append, using the old shard length from `shard_bytes`. The trailing shard is longer, the range lands on the wrong bytes and the index checksum fails. A reload fixes it.

An unsharded store has no shard index, so a stale viewer has no such failure.

The same mismatch can occur for up to the short lifetime when a CDN holds an old shard object next to a new `zarr.json`, or the reverse. This is why the upload order is shards, then metadata, and why both lifetimes are the same.

### Doctor on an appended store

`chronozarr doctor` on an appended store passes the same checks. Its `cache-control` line reads the header of one object, `0/data/c/0/0/0/0` (cell (0, 0)):

- In an unsharded store, that object is the chunk of timestep 0. It is immutable for good.
- In a sharded store, it is time shard 0. It is immutable once a second time shard exists.
- While a sharded store has one time shard, that object is the trailing shard with `max-age=300`. Doctor warns on a versioned prefix. The warning is expected then.
