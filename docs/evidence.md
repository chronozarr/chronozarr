# Evidence

This file holds measurements, tested versions, dates and caveats. User docs link here. Each section covers one topic.

- [Reader checks](#reader-checks)
- [Layout choice](#layout-choice)
- [Codec decode](#codec-decode)
- [v0.3 against v0.2](#v03-against-v02)
- [Viewer measurements](#viewer-measurements)
- [Appending](#appending)
- [PNG frames](#png-frames)
- [NISAR and SWOT example data](#nisar-and-swot-example-data)
- [Embedding](#embedding)
- [MapLibre layer](#maplibre-layer)
- [JavaScript dependencies](#javascript-dependencies)
- [Comparison notes](#comparison-notes)
- [Live store and publishing](#live-store-and-publishing)
- [Hosting observations](#hosting-observations)
- [Sentinel-2 ingest performance](#sentinel-2-ingest-performance)

## Reader checks

GDAL 3.13.3 read the two-level v0.3 fixture. It gave correct EPSG:32618, transforms and all six oracle values, without warnings. It did so both with and without `_CRS`. Selected raster slices did not expose automatic overviews.

CarbonPlan zarr-layer 0.10.0 with zarrita 0.7.5 rendered the fixture. It needed no CRS, bounds or spatial-dimension overrides. It returned 1107 at the checked pixel centre.

Separate-mask handling and framebuffer color calibration were not established.

xarray needs an explicit level group. `xarray.open_zarr(store, group="0")` opens a selected level with ordinary stored values. `open_zarr` on the store root may return an empty dataset.

GDAL 3.12.4 needs `_CRS` to assign the CRS. GDAL 3.13.3 assigns the CRS without that alias. In the sliced form of the first check, GDAL 3.13.3 exposes the tested pyramid as subdatasets and does not attach the levels as overviews. Subdataset enumeration does not show that GDAL applies a separate mask. The check of 2026-10-09 below opens the whole data array and finds the overviews.

On 2026-10-09 another check used GDAL 3.13.3 (`ghcr.io/osgeo/gdal:ubuntu-small-3.13.3`, digest `sha256:64250faf833c06d4b21afce4c27190039ba7ab58d70f0eebc87cf77d929c0b40`, arm64) on the fixture from `scripts/spike_v03_fixture.py`. GDAL 3.13 maps the zarr-conventions multiscales to overviews (GDAL pull request 13736, merged 2026-01-26, milestone 3.13.0).

- `gdalmdiminfo -array /0/data` lists `"overviews": ["/1/data"]`.
- `gdalinfo 'ZARR:"<store>":/0/data'` prints `Overviews: 4x4` on all 6 bands (3 dates by 2 bands). `/0/mask` has overviews too.
- `gdalinfo 'ZARR:"<store>":/0/data:1:1'`, a single slice, shows no overviews. The sliced view drops them.

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

The cache held roughly half of the 117 timesteps at that size. That count dates from 2026-09-30, before the cache budgets changed on 2026-10-01 (commit `33c4444`). Since then the cap is 1.5 GiB for the decoded and compressed tiers together. A machine that reports less than 8 GB of memory gets 768 MiB. Idle prefetch stops after 64 MiB and expands only during playback. A 4-cell view fits entirely and plays at the display rate.

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

### Mixed states during an update

2026-10-09, zarr 3.1.6, a synthetic 50 x 70 pixel store appended from 4 to 6 timesteps, copied to local directories (no host). Each directory mixes objects of the two states, as a reader could see them between two puts of `chronozarr publish --update`.

| Mix | Result |
|---|---|
| Root and array metadata at 4 timesteps, `0/time/c/0` at 6 | `zarr.open_group(...)["0/time"][:]` raises `ValueError: cannot reshape array of size 6 into shape (4,)`, with and without consolidated metadata |
| Same mix | `chronozarr.open_store(...).to_xarray(lod=0)` reads 4 timesteps: it takes `times` from the root |
| Level 0 `zarr.json` files at 6 timesteps, root at 4 | With consolidated metadata, `0/data` has shape (4, ...). Without it, `0/data` has shape (6, ...) next to 4 root `times`, and reading `0/time` raises `ValueError` |

The update tests in `tests/test_publish_update.py` run against a fake bucket. The update has not run against AWS S3 or R2.

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

## NISAR and SWOT example data

On 2026-10-09 the source files of `examples/nisar`, `examples/swot_raster` and `examples/swot_intensity` were matched to public granules. The CMR queries read metadata only. No Earthdata login was used and no granule was downloaded.

### NISAR example

The two `.npz` extracts carry no product ID. The README names the only granules that CMR returns for the window on the two dates.

- The window is x 512270 to 523430 m and y 888070 to 904510 m in EPSG:32618. Its centre is at -74.84 degrees longitude and 8.11 degrees latitude.
- A query of `NISAR_L2_GCOV_PROVISIONAL_V1` at that point on 2026-06-22 and on 2026-08-21 returned one granule for each date. The same query of `NISAR_L2_GCOV_BETA_V1` with the window box returned none.
- Both granules are track 119, frame 6, ascending, mode 2005, dual-polarization HH and HV, product version 1.0.11. They are cycles 23 and 28, 60 days apart.
- Both footprints contain the four corners of the window.
- The extract arrays were not compared with the granules, because the download needs an Earthdata login. The match rests on the date and the footprint.
- `extract_window.py` was run on synthetic HDF5 files that have the group and dataset names of the GCOV grid. It was not run on a real granule.

### SWOT raster example

The two NetCDF files in the author's directory have the MD5 checksums that CMR lists for `SWOT_L2_HR_Raster_100m_D` (`dd574a747bd9e09d97a4e4f5e70d289e` for 2025-11-14 and `ba558cc86cd6c7622ccb2e50f4938854` for 2026-07-12). This query returns both granules:

```sh
curl -sG https://cmr.earthdata.nasa.gov/search/granules.csv \
  -d short_name=SWOT_L2_HR_Raster_100m_D \
  -d 'readable_granule_name[]=*UTM18S_N_x_x_x_041_369_109F_20251114T234531*' \
  -d 'readable_granule_name[]=*UTM18S_N_x_x_x_053_076_046F_20260712T211127*' \
  -d 'options[readable_granule_name][pattern]=true'
```

- The default store is `data/stores/swot_roanoke/local-20261002`. WSE is in metres above the geoid of the source product, with its delivered corrections.
- The build intersects the aligned EPSG:32618 grids. It searches the overlap at a stride of 64 pixels for the 512 x 512 window with the most shared valid pixels. It stages GeoTIFF crops with no warp and no interpolation.
- Valid pixels fall from 93,717 to 26,049 on 2025-11-14 and from 65,672 to 30,589 on 2026-07-12 with `--quality good`. With `--quality usable` they fall to 76,521 and 59,971. The build never excludes a negative value.
- The build checks level-0 float bit patterns and masks, xarray values, and the values, masks, units and grid of a COG export. Both xarray interfaces keep the units in `band_units` and set the variable units to `m`.
- The build writes source paths, SHA-256 hashes, windows and counts to `data/reports/swot-roanoke-20261002.json`, with `-good` and `-usable` versions. All three stores pass these checks and the browser check.
- The browser check compares chunk and mask hashes with Python. It checks negative readouts and units on both dates. It compares the MapLibre `getValueAt` with a source pixel and saves `data/reports/swot-roanoke-viewer.png`.
- The MapLibre demo accepts `p=band` for this store, and the leafmap helper uses `product="band"`.
- The player reads the local store through a range and CORS server, so the browser may ask for local-network permission. The notebooks turn off the floating sidebar of leafmap, as the [leafmap example](../examples/leafmap/README.md) explains.

### SWOT intensity example

The `source` tags of the two 5 m GeoTIFFs name `SWOT_L1B_HR_SLC_025_244_102R_20241211T161411_20241211T161422_PGD0_01.nc` and `SWOT_L1B_HR_SLC_032_244_102R_20250506T172942_20250506T172953_PGD0_01.nc`. The local SLC files of these names have the MD5 checksums that CMR lists for `SWOT_L1B_HR_SLC_D`: `0b79d78429ba02b47a9590a219fa21da` for 2024-12-11 and `67a1551ff4d035c45558fa152a7306f8` for 2025-05-06. Each file is 2.0 GB.

That collection also holds `SWOT_L1B_HR_SLC_032_244_102R_20250506T172942_20250506T172953_PID0_01.nc`, a different file for the same pass. Only the `PGD0` name matches the tag. The 60 m and 5 m GeoTIFFs are outputs of `swot-slc-geocode`, a private project, and no archive holds them.

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

These numbers were recorded with the layer on 2026-09-30 (commit `bdc5938`). They were not repeated after 2026-10-01 (commit `33c4444`), when the reader began to cap idle prefetch at 64 MiB per idle episode. The 260 MB figure below predates that cap. The first view fetches the coarsest level first. On a 3 MB/s link with 120 ms latency, for 4 cells at level 0 of the Ucayali store (12 MB), the first pixels appeared after 2.5 s instead of 5.5 s. The full-detail view was ready after 7.6 s instead of 5.8 s. On a fast link neither differed.

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

Without `Timing-Allow-Origin`, `PerformanceResourceTiming.transferSize` is 0 for cross-origin requests. A benchmark that reads resource timing then under-reports bytes. The reader counts the body bytes of each response it receives, so the viewer's own byte counters do not depend on the header.

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

## Local preview and remote notebooks

Checked on 2026-10-09 on macOS with Python 3.11 and a synthetic store of 6 timesteps, 2 bands and 200 by 220 pixels.

### Self-hosted viewer

`chronozarr preview STORE --viewer-dir DIR` served a viewer folder written by `chronozarr-viewer`. The viewer opened at `http://127.0.0.1:<port>/_viewer/demo/index.html`, read the store from the same server and painted the first timestep.

### Proxy route

A stand-in for `jupyter-server-proxy` removed the prefix `/user/ada/proxy/<port>` and answered 403 without a login cookie. With the cookie, the viewer opened at `/user/ada/proxy/<port>/_viewer/demo/index.html?store=/user/ada/proxy/<port>/store`. It made 103 store requests through the proxy and painted the first timestep. The notebook player (`player.js`) also reached `ready` with the two paths on the proxy origin. This was not run on a live JupyterHub or with the real `jupyter-server-proxy`.

### Port in use

With `--port` set, `chronozarr preview` fails when anything listens on the port. A `python -m http.server --bind 0.0.0.0` process on the port gave the error "port 8795 on 127.0.0.1 is already in use". A plain bind on `127.0.0.1` next to a listener on `0.0.0.0` succeeds on macOS when `SO_REUSEADDR` is set, so the server also connects to the port before it binds.

### Interrupt

A SIGINT stopped the command with exit status 0 after it printed "stopped". The same port was free for a new bind afterwards.

## Sentinel-2 ingest performance

Measured 2026-10-10 on one machine: Apple M3 Max laptop (16 CPUs, 128 GB), macOS, home connection in Chapel Hill, GDAL 3.12.1 from the rasterio 1.5.0 wheel, numpy 2.4.4. The pipeline is `examples/sentinel2_pc/mosaic.py`; the baseline is the same file at commit `01185cd`. Cross-machine performance has not been tested. The harness (`examples/sentinel2_pc/bench.py`) records the machine with every result, so the same commands can be run elsewhere unchanged.

### Method

Each workload is a frozen scene list (`bench.py freeze`, scene IDs and a SHA-256 in `data/bench/workloads/`). Every measurement is a fresh process (`bench.py run`); `bench.py compare` runs every workload and configuration once per repetition, in a new random order each repetition. Wall time runs from the pipeline call to the last `.npz` on disk; scene search is excluded. Requests and bytes come from GDAL's own network statistics. Peak memory is the process maximum RSS. Tables give the median and interquartile range of 3 repetitions (2 for the 10 MB/s lab case). A difference smaller than the spread of the network is reported as no difference. Correctness is the SHA-256 of each month's `bands` and `coverage` arrays, compared with the baseline's: all 111 new-pipeline runs in the final data (137 runs with the baselines) were bit-exact. Earlier throttled lab runs that were not are described under "Read retries"; their records are kept in `data/bench/archive/`.

`fixed-N` is N concurrent reads without adaptation. `auto` is the default: 16 concurrent reads, lowered when the host fails or throttles. `auto-max32` is auto mode with `--max-requests 32`, which also climbs above 16. `auto-capped` is auto with `--max-requests 8 --cpu-workers 4 --memory 2GB`. The baseline reads 4 scenes at a time, each scene's 5 files one after another.

### Planetary Computer over a home connection

The runs in this section and the next predate two later changes from the constrained runs in [bench/s2-ingest/portability.md](../bench/s2-ingest/portability.md). Band reads now fill the month buffer in place and masking works in row blocks, which lowered peak RSS in Docker from 2.8-3.0 to 2.4 GB for the 20-scene month. Auto mode now starts at 4 reads per CPU when that is below 16, which does not change anything on this 16-CPU machine. Output and concurrency here are unaffected; the peak RSS columns are higher than the current code reaches.

| Workload | Scenes | Baseline | fixed-4 | fixed-16 | auto | auto-max32 | Peak RSS baseline / auto |
|---|---|---|---|---|---|---|---|
| Ucayali, 1 month | 10 | 44.1 s [42.8-44.5] | 26.4 s | 20.9 s | 22.8 s | 21.5 s | 10.6 / 2.7 GB |
| Ucayali 5 km box, 3 months | 15 | 19.6 s [19.0-19.8] | | 4.2 s | 4.3 s | 5.0 s | 0.39 / 0.31 GB |
| Ucayali, other UTM zone (warp path) | 10 | 45.3 s [44.9-47.5] | | 20.2 s | 22.5 s | 24.8 s | 10.9 / 2.9 GB |
| Lake Mead, 1 month, 4 tiles | 16 | 34.9 s [34.1-35.0] | 20.3 s | 14.0 s | | 14.8 s | 12.7 / 3.0 GB |
| Yukon delta, 3 sparse months | 11 | 145.4 s [142.6-152.3] | 69.2 s | 48.4 s | | 41.8 s | 14.5 / 4.6 GB |

The new pipeline is 1.9 to 4.7 times faster than the baseline at its default settings (auto, or fixed-16, which auto equals when no read fails). At the baseline's concurrency (fixed-4), the pipeline changes alone give 1.7 to 2.1 times. The rest comes from concurrency: 16 reads beat 4 on every workload. Auto and fixed-16 are indistinguishable; auto-max32 is about 15 % slower on the short runs and about 14 % faster on the 3-month Yukon run, so the default ceiling is 16. Downloaded bytes fell 13 to 26 % on the native-grid workloads (Yukon: 1663 to 1289 MB, 1031 to 837 requests) from reading SCL first and skipping source blocks without a valid pixel; the warp path skips only whole scenes and read the same 499 MB as the baseline.

Three of these runs retried a failed read (Ucayali fixed-16, warp auto-max32 and Yukon auto-capped; they are the top of each spread, up to 101 s). They predate the fix under "Read retries" and were still bit-exact.

### Local server with traffic shaping

`bench.py lab-workload` serves a full mirror of the Ucayali month (9.7 GB) from `labserver.py`, with each scene under two URLs, so a month has 20 scenes. The server adds latency, a shared bandwidth cap, or HTTP 503 for requests beyond a number in flight.

| Condition | Baseline | fixed-4 | fixed-16 | fixed-32 | auto | auto-max32 |
|---|---|---|---|---|---|---|
| No shaping | 18.1 s | 3.4 s | 2.3 s | 2.5 s | | 2.3 s |
| 50 ms per response | 28.0 s | 12.0 s | 4.3 s | 3.3 s | | 4.3 s |
| 20 ms, 10 MB/s shared | 115.1 s | 89.3 s | 88.3 s | | | 88.2 s |
| 50 ms, 503 above 12 in flight | 29.6 s | 16.8 s | 18.3 s | 18.0 s | 18.9 s | 18.2 s |

Without shaping the new pipeline is 7.9 times faster and peak RSS falls from 16.9 GB to 4.3 to 4.8 GB. With 50 ms latency, 32 reads beat 16, but the run lasts 4 s and auto-max32 does not climb within it. With a 10 MB/s cap every configuration waits on the link, and the 1.3 times gain is the 13 % fewer bytes plus overlap. The throttled host costs every new configuration about the same, and none drops a scene.

### What changed and what each change bought

- Reading SCL first and skipping bands of scenes, and source blocks, with no valid pixel: 13 to 26 % fewer bytes on the native-grid workloads. The windowed read on the native grid equals bilinear `reproject` bit for bit when the scene's CRS is the AOI's and the origins differ by whole pixels, which holds for Sentinel-2 in its own UTM zone (`tests/test_ingest_mosaic.py`, and on real Planetary Computer files).
- One read per asset in a shared pool instead of one thread per scene reading 5 assets in turn, and months that overlap within a memory budget instead of a barrier per month.
- The median over scenes on uint16 with invalid pixels as 0, in parallel row blocks: 0.20 s instead of 12.7 s for 16 scenes of 2765 x 2759 pixels, equal to float32 `nanmedian`.
- An explicit GDAL block cache (64 to 512 MB) instead of GDAL's default of 5 % of RAM, and a uint16 stack instead of float32: peak RSS 10.6 to 2.7 GB for one Ucayali month.
- `.npz` written at zlib level 1 instead of 6: 0.8 s instead of 2.6 s per month, files 2 % larger, same arrays. Without shaping this was 65 % of a 1-month run.
- Writes to a temporary name and a rename, so an interrupted run never leaves a partial month. A SIGINT during a 10 MB/s lab run exited after the 16 reads in flight drained (14.6 s) and left no file.

### Read retries

GDAL remembers a failed open of a URL for the life of the process. A second open of the same URL fails at once with the first error and sends no request, until `VSICurlPartialClearCache` drops the entry (checked by serving one URL from a server that always answers 503, then from a healthy one on the same port). A Planetary Computer signed URL is the same until its token refreshes, so the baseline's retries of a scene could not reach the host after a failed open; they could only succeed when the first failure came after the open. The audit below finds no missing scene in the current Ucayali months. Before the fix, in the throttled lab case, fixed-32 dropped one scene in 2 of 3 runs (66.6 s median, output not bit-exact) and auto in 1 of 3. The pipeline now clears the entry before every retry, and the same case was bit-exact in all 15 new-pipeline runs (`tests/test_ingest_mosaic.py::test_pipeline_recovers_from_a_transient_error_on_every_asset`).

GDAL retries most HTTP 503 responses by itself (`GDAL_HTTP_MAX_RETRY`), so they never surface as errors. They appear as warnings on the `rasterio._env` logger, which auto mode counts. One read is several HTTP requests, so even 4 reads hit a limit of 12 in flight now and then. Halving at every second 503 drove the request limit down to 1 and made auto the slowest configuration. Auto now lowers the limit by a quarter only when an epoch's 503s outnumber its completed reads, and halves it when reads fail outright.

### Ucayali scene audit

Earlier full-archive runs dropped scenes after read errors (throttling, and expired tokens before the fix of 2026-09). `examples/sentinel2_pc/audit_scenes.py` checks saved months without downloading any band. A month's `coverage` is k/n, where n counts every scene searched and k only those read and valid. The script repeats the scene search, reads each scene's SCL band, predicts k per pixel and attributes any shortfall to specific scenes. In a test with 7 scenes removed by hand from the counts of 4 months, it named exactly those 7 scenes. Two clear acquisitions of one tile ten days apart, with 99.6 % of their valid pixels in common, needed the thresholds at the measured noise level (99.9 % and 0.05 %) to be told apart.

Result for the 117 Ucayali months on disk (written 2026-09-29 and 30), checked 2026-10-10:

- The scene count n stored in every month equals today's search, so the scenes searched then and now are the same: 739 scenes.
- No scene with a valid pixel is missing. The largest unexplained shortfall in any month is 694 of 7,628,635 pixels. These are pixels where SCL is valid but a band is 0; the pipeline excludes them, and the SCL-only prediction does not see them. No pixel has more valid scenes saved than predicted.
- Six scenes have fewer than 1000 valid pixels (24 to 560), below what the shortfall test can resolve. None of their pixels shows a shortfall, so they were included.
- Eight scenes have no valid pixel in the AOI. Whether they were read cannot be told, and it does not change any value.
- The local v0.3 store `data/stores/ucayali_santa_maria_v03` equals the months on disk bit for bit in all 117 time steps. The published copy was not downloaded to compare.

No month needs to be rebuilt. The per-month report is `bench/s2-ingest/audit-ucayali_santa_maria.json`.

### Lake Mead scene audit

The 127 Lake Mead months on disk were written on 2026-04-19, by the pipeline as it was then. The same audit (2,318 scenes, `bench/s2-ingest/audit-lake_mead.json`) finds every month up to 2023-05 complete. From 2023-06 on, 674 scenes with valid pixels are missing in 34 months:

- In the 33 months from 2023-07 to 2026-03 no scene contributed at all. Some of them also have scenes without a valid pixel, which is why the audit lists fewer missing scenes than scenes for 9 of them (2023-08, 2023-12, 2024-01, 2024-04, 2024-06, 2025-03, 2025-08, 2025-11, 2026-01). Every one of the 33 is a carry-forward copy of an earlier month; the `water-1` build skipped all 33 as having no valid pixel.
- 2023-06 is the only partial month: at least 20 of 24 scenes are missing, and 2.3 million pixels of shortfall remain unattributed.

The pattern, intact months followed by failures in calendar order, matches the expired SAS tokens described in the napkin for the first Ucayali run. A second defect predates the audit: the +1000 offset of processing baseline 04.00 and later is not removed. The rule goes by processing baseline, not acquisition date, and ESA reprocessed some older acquisitions: 4 scenes of 2019-03 (baseline 05.00) and 4 of 2021-12 (04.00) carry the offset, and every scene from 2022-01 on does. Checked against `BOA_ADD_OFFSET` in 23 product-metadata files and against fresh reads of B04 on a desert target (June 2022: stored median 3882, offset removed 2882, June 2021 2765). `catalog.py` applies the rule correctly today; the months predate that code (2026-09-30). Carry-forward spreads the 2019-03 values into gap pixels of every month to 2021-11 (0.77 % of pixels in 2019-04, falling to 0.01 %). So every month from 2019-03 to 2026-03 (85 months, about 30 to 41 GB to read again) and the `lake_mead/water-1` store built from them need rebuilding; nothing was rebuilt here. The manifest, the evidence and the rebuild procedure are in `bench/s2-ingest/lake-mead-recovery.md` (branch `data/lake-mead-recovery`). The other AOIs have stores but no monthly files on disk; their coverage is a 0/1 flag, which cannot attribute missing scenes.

### Spatial strips

Measured 2026-10-10 on the same laptop, Docker VM and home connection as above. Branch `perf/s2-ingest-tiles`; the comparator ("PR #90") is the pipeline at `d7c2ce1`, run by `bench.py --baseline-ref d7c2ce1` with its own default settings. Records: `bench/s2-ingest/results/strips/` and `bench/s2-ingest/runs/*strips*.json`.

A month whose scene stack does not fit the memory budget is composited in full-width strips of the AOI grid, with heights in multiples of 512 rows; the planner picks the tallest strip that fits. When the whole month fits, it is one strip, the earlier path. Monthly files became tiled GeoTIFFs written strip by strip, so no month is held whole at any point, and the encode step reads them one 512 by 512 cell of every month at a time.

**Exactness.** Every step after the read is per pixel. Native-grid reads of a strip are bit-exact, so every native-grid record (all lab workloads, all strip heights, all Docker limits, 14 Planetary Computer runs) has the same month hashes as PR #90 and the original pipeline. Warps are not chunk-independent in GDAL by default, for three reasons, each checked on a real scene (2834 and 9202 pixels square, EPSG:32718 to 32719):

- GDAL derives the bilinear scale of each warp chunk from the chunk's shape. Strips of 256 rows differed from the whole grid by up to 1049 DN. Pinning `XSCALE`/`YSCALE` to the pixel-size ratio removes this.
- GDAL's approximate transformer (error up to 0.125 source pixels) interpolates along each destination row over the chunk's width. Column splits changed 4.1 million of 8 million pixels; full-width strips changed none.
- GDAL splits a warp chunk where the source covers only part of it (`SRC_FILL_RATIO_HEURISTICS`), which is the normal case for a scene from the neighbouring UTM zone, and it splits large grids by memory. On the 9202-pixel grid this made the whole-grid default warp differ from any strip on 0.4 % of pixels.

The pipeline therefore warps each full-width strip in one chunk (`warp_mem_limit` 8192 MB, which is a threshold, not an allocation; heuristics off; scale pinned). Then any strip height gives the whole grid's pixels: the lab warp month has one hash across the whole grid and every strip height.

**Which warp is correct.** Comparing PR #90 with strips showed 0.05 % of pixels whose set of valid scenes changed, so the SCL warp was checked against an independent reference: every grid pixel centre transformed to the source CRS with pyproj, and the 20 m SCL pixel containing it, for the 10 real scenes of the warp month.

| SCL warp | Class differs from the reference | Validity differs |
|---|---|---|
| GDAL default (PR #90) | 0.122 % | 0.022 % |
| pinned scale, one chunk, approximate transformer (first strip version) | 0.165 % | 0.033 % |
| exact transformer (`tolerance=0`) | 0 | 0 |

Every misplaced pixel lies within 0.08 source pixels of a source pixel edge: the approximate transformer's error (up to 0.125 source pixels) moves the sample into the neighbouring 20 m pixel. Both earlier versions misplaced observations, in different places. Since the SCL decides which scenes count at a pixel, it must be exact. GDAL's exact transformer costs 22 times the approximate SCL warp, tolerances of 1e-2 to 1e-5 still left 5030 to 8 misplaced pixels, and rasterio before 1.5 (the version the lock resolves on Python 3.11) cannot request it: it passes `tolerance` to GDAL as a warp option, which GDAL ignores, and CI's Python 3.11 job found the 9 misplaced pixels of the approximate transformer. So the SCL is not warped by GDAL at all: the window's pixel centres are transformed exactly into the source CRS once per window and source CRS (`rasterio.warp.transform`, 2.3 s for the whole Ucayali grid), and each scene takes the source pixel containing each centre (`read_nearest`). On the real warp month this is bit-identical to GDAL's exact transformer, on every rasterio version. Band values only carry interpolation error, so bands use a transformer tolerance of 0.01 source pixels. Against exact band warps, the default left 60 % of values off (median 1 DN, p99 20, max 160) and 0.01 leaves 10 % off (p99 2, max 11) at the same cost; exact bands would cost 7 times. `tests/test_ingest_mosaic.py` builds a checkerboard SCL with a class edge at every source pixel edge and requires the pyproj pixel everywhere; with the old 0.125 tolerance it finds 9 misplaced pixels.

Result on the 20-scene warp month, against a pipeline with every warp exact: the valid-scene count differs on 1 of 7,927,028 pixels (a band's zero/non-zero changed where the bilinear kernel meets the band's nodata area, resolved to 0.01 source pixels), and band values differ by mean 0.15 DN, p99.9 3, max 21. PR #90's count differs from the exact one on 10,401 pixels (0.131 %), with value changes up to 2693 DN there. Warped scenes cost more: the all-warped lab month takes 6.0 s instead of 2.9 s on 16 CPUs (39 instead of 31 CPU seconds; the coordinate transformation holds the GIL, so it does not spread over threads), and 22.9 s instead of 20.8 s in Docker with 2 CPUs. Native-grid scenes never warp and are unchanged. The speed table below was measured before this change. After it (2 runs each, `results/strips/exact-scl/`): native-grid workloads are unchanged and bit-exact (20 scenes: whole grid 1.65 s, PR #90 2.38 s; 4 months: 3.04 and 5.84 s); the all-warped month took 5.7 s against 3.9 s for PR #90 on 16 CPUs, and 52 s instead of 21 s in Docker with 2 CPUs, while the SCL went through GDAL's exact transformer once per scene; computing the exact coordinates once per window brought the 2-CPU case to 22.9 s (above). In Docker with 1 CPU and 1.5 GB the warped month is refused before any download (a 512-row strip needs 1.09 GiB with warped reads, the budget is 1.06 GiB).

**Memory.** The first strip runs exceeded their budget (1000 MiB budget, 1.35 GB peak RSS): masking and the median worked in fixed row blocks, about 56 MB of temporaries per CPU worker for a 20-scene month 2759 pixels wide, times 15 workers. Both now work in blocks of 2 MiB, and the estimate counts the workers' temporaries and GDAL's 5 MB per-file cache of each read. A cgroup or SLURM limit is now budgeted at 75 % instead of 50 %, since it is memory set aside for the job.

| Run (20-scene lab month unless noted) | Budget | Estimate | Peak RSS | Strips |
|---|---|---|---|---|
| macOS, 16 CPUs, `--memory 1500MB` | 1.46 GiB | 1.45 GiB | 1.35 GiB | 3 |
| macOS, `--memory 2000MB` | 1.95 GiB | 1.81 GiB | 1.62 GiB | 2 |
| macOS, whole grid | 24.5 GiB | 2.98 GiB | 2.39 GiB | 1 |
| Docker 2 CPUs, 2 GB | 1.46 GiB | 1.34 GiB | 0.90 GiB (cgroup 1.09) | 3 |
| Docker 1 CPU, 1.5 GB | 1.06 GiB | 0.98 GiB | 0.72 GiB (cgroup 0.87) | 6 |
| Docker 2 CPUs, 3 GB (50 % budget) | 1.48 GiB | 1.35 GiB | 0.92 GiB (cgroup 1.10) | 3 |
| Docker 4 CPUs, 4 GB (50 % budget) | 1.98 GiB | 1.72 GiB | 1.09 GiB (cgroup 1.33) | 2 |
| Docker 1 CPU, 2 GB, 4 months | 1.46 GiB | 1.31 GiB | 1.17 GiB (cgroup 1.50) | 2 |
| Docker 2 CPUs, 2 GB, warp month | 1.46 GiB | 1.13 GiB | 0.93 GiB (cgroup 1.08) | 6 |

The estimate was above the process's peak RSS in every run, by 7 to 45 % (scenes without valid pixels never touch their zeroed planes). The cgroup peak also counts the lab server and the page cache. PR #90 refused every one of the Docker cases at its default budget (the month needs 2.35 GiB); with `--memory 2600MB` set by hand it ran the 2 CPU, 3 GB case in 7.8 s at a 2.54 GB cgroup peak, against 7.7 s and 1.10 GB for strips by default. A 1 GB container is refused before any download: a 512-row strip needs 0.98 GiB, mostly the fixed 0.5 GiB allowance for the interpreter and libraries.

**Speed** (lab server on the mirror, median of 3, 2 for shaped links; bytes and requests as served):

| Workload, link | PR #90 | Whole grid | 1024-row strips | 512-row strips | `--memory 1500MB` |
|---|---|---|---|---|---|
| 5 km box, 3 months | 0.31 s | 0.30 s | 0.30 s (1 strip) | 0.30 s (1 strip) | 0.29 s (1 strip) |
| 20 scenes, no shaping | 2.24 s, 3.28 GB, 870 MB | 1.51 s, 2.43 GB, 870 MB | 1.51 s, 2.01 GB, 1030 MB | 1.83 s, 1.81 GB, 1347 MB | 1.75 s, 1.44 GB (3 strips) |
| 20 scenes, 50 ms | 4.36 s | 3.71 s | 3.59 s | 4.82 s | 4.22 s |
| 20 scenes, 20 ms, 10 MB/s | 88.3 s | 87.5 s | 103.3 s | 134.8 s | |
| 20 scenes, 50 ms, 503 above 12 in flight | 22.0 s | 19.1 s | 15.1 s | | |
| 4 months, no shaping | 5.24 s, 4.04 GB | 2.72 s, 3.90 GB | 2.99 s, 2.16 GB | 3.52 s, 1.37 GB | 4.10 s, 1.51 GB (2 strips) |
| 4 months, 50 ms | 7.23 s | 6.29 s | 6.30 s | 8.81 s | 8.05 s |
| warp month, no shaping | 3.53 s, 3.69 GB, 997 MB | 2.96 s, 2.74 GB, 997 MB | 2.89 s, 1.80 GB, 1176 MB | 3.13 s, 1.65 GB, 1516 MB | 3.48 s, 1.21 GB (6 strips) |

The whole-grid path is faster than PR #90 mainly because a zstd GeoTIFF is cheaper to write than a zlib `.npz`, and the bounded temporaries lowered its peak RSS. Strips cost bytes, not results: a source block cut by a strip edge is read once per strip (18 % more bytes at 1024 rows, 55 % at 512). On a capped link the time follows the bytes (10 MB/s: +18 % and +54 %); on an uncapped link 1024-row strips cost nothing measurable. Header requests are not repeated, since GDAL keeps a file's size and header across reopens; a larger GDAL download cache (`CPL_VSIL_CURL_CACHE_SIZE` up to 1 GB) did not avoid re-reading the cut blocks (1348 to 1333 MB). The first strip is on disk well before the month: 46 s instead of 87.5 s at 10 MB/s with 512-row strips. Under the 503 throttle every configuration retried through GDAL (49 to 75 HTTP retries per run) and stayed bit-exact.

**Planetary Computer** (home link, `ucayali-1m`, 10 scenes, 14 runs, interleaved and alternating): PR #90 11.7 to 18.6 s (3 runs), whole grid 11.7 to 28.6 s (5), 1024-row strips 11.1 to 55.2 s (6); 435 MB for the whole grid and 508 to 510 MB in strips. Every output was bit-exact. Three of the six strip runs hit DNS failures ("Could not resolve host: sentinel2l2a01.blob.core.windows.net", 12 to 24 failed reads each); none of the eight others did. The retries recovered every scene, but repeated failures halve the request limit, so those runs averaged about 2.4 concurrent reads and took 44 to 55 s. The three clean strip runs took 11.1, 14.1 and 18.5 s. 640 concurrent lookups through the system resolver all succeeded, so the cause is not established; strips open each asset once per strip, which may make more name lookups than the whole grid.

The DNS failures were then reproduced and instrumented on a small scale (SCL reads of the same month, 16 threads, curl verbose log), not with more full runs. Strips do not make more lookups: per run they made 32 fresh connection attempts and reused 54 connections, against 40 and 20 for the whole grid, because GDAL keeps connections per thread across reopens; in 17 alternating repetitions of each, the only failure was in a whole-grid run, at the start, while 16 threads resolved the host with an empty cache. The host is a CNAME chain through Azure Traffic Manager with TTLs of 7 and 17 s, so long runs resolve it cold many times; 144 concurrent lookups through the system resolver at expired TTLs all succeeded, so what fails first is not established. What made the failures expensive is established: libcurl 8.16 to 8.21 caches a failed name resolve, transient or not, for half of its 60 s DNS cache timeout (curl documentation of `CURLOPT_DNS_CACHE_TIMEOUT`), rasterio 1.5 bundles libcurl 8.17.0, and GDAL keeps one connection cache per thread, where the retry runs. The three reads of the failing run kept failing on attempts 1, 3 and 4 over about 40 s while other threads read the same host, and every failure counted against the request limit. A resolve failure now waits 31 s before its retry and does not lower the limit (#90). With 3 of 6 strip runs and 0 of 8 others failing (Fisher p about 0.05) and no mechanism tying strips to lookups, the failures are not attributed to strips.

**Encode.** On the 117 Ucayali months on disk (legacy `.npz`), PR #90's encode stacked all months: 30.5 s and 10.2 GB peak RSS. The lazy reader took 56.6 s and 3.3 GB, of which 29.9 s copied the legacy months to temporary GeoTIFFs one at a time (months written by the new pipeline skip that). The two stores are equal in every value of every level, time step, band and coverage. What remained is chronozarr's own encoder, which held a cell of every time step in flight; #99 walks unsharded stores in time slabs of about 64 MiB per cell. With it, the same 117 months encode at 1.34 GB peak RSS (32.6 s for the encode step), and the stores from PR #90, from the lazy reader and from the lazy reader with slabs are byte-identical in all 11,574 files.

### Tried and not kept

- `GDAL_NUM_THREADS` of 2 or more on a partial window: GDAL fetches the needed tiles in one multi-range batch, 22 % fewer bytes (486 against 595 MB, 184 against 386 requests on the 6-scene Yukon month) but 30 to 45 % longer wall time at 8 concurrent reads on the home link. GDAL stays at its default of 1.
- `CPL_VSIL_CURL_USE_HEAD=NO`: the HEAD becomes a GET, so the request count does not change.
- Climbing above 16 reads by default: no net gain on the home link (see above).
- A larger GDAL download cache (`CPL_VSIL_CURL_CACHE_SIZE` 256 MB and 1 GB) so that strips reuse the source blocks their edges cut: 1333 instead of 1348 MB on 512-row strips. Not kept.
- GDAL's exact transformer (`tolerance=0`) for warps, which would allow windows narrower than the AOI: 5.5 times the warp CPU. Not kept; strips span the full width.
- An early sweep on the home link (24 asset reads, 4 to 32 workers, 2 repetitions) showed no effect of concurrency, 11 to 18 MB/s. The interleaved runs above, with 3 repetitions per cell over two sessions, consistently favoured 16 over 4.

### Remaining opportunities

| Opportunity | Evidence | Effort |
|---|---|---|
| Split one large window into stripes read in parallel, for months with few scenes | one 90 MB read: 10.6 and 11.6 s whole, 7.8 and 9.9 s in 4 stripes; Yukon slots were 46 % busy with 2 scenes | half a day, plus a bit-exact check of the stitched read |
| Multi-range fetch (`GDAL_NUM_THREADS` >= 2) on metered or capped links | 22 % fewer bytes, slower here | a flag and one measurement on a capped link |
| `reproject(num_threads=...)` on the warp path | the warp path is the slowest workload; GDAL's multithreaded warp splits the output into chunks, so equality with the single-threaded warp must be checked | a few hours with the existing equality test |
| Several writer threads | the single `.npz` writer caps a fast link at about 75 months per minute at Ucayali size | an hour |
| Benchmarks on a second machine, a Linux HPC node and a cloud VM | only this laptop has been measured | `bench.py` runs unchanged |
| Single-band and other band sets | the pipeline reads B02, B03, B04, B08 and SCL only, so the harness has no single-band case | depends on making the band list a parameter |
