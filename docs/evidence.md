# Evidence

This file holds measurements, tested versions, dates and caveats. User docs link here. Each section covers one topic.

- [Reader checks](#reader-checks)
- [Layout choice](#layout-choice)
- [Codec decode](#codec-decode)
- [v0.3 against v0.2](#v03-against-v02)
- [Viewer measurements](#viewer-measurements)
- [Appending](#appending)
- [Live store and publishing](#live-store-and-publishing)
- [Hosting observations](#hosting-observations)
- [Comparison notes](#comparison-notes)
- [JavaScript dependencies](#javascript-dependencies)
- [Development checks](#development-checks)
- [Stale text found in the old README](#stale-text-found-in-the-old-readme)

## Reader checks

GDAL 3.13.3 read the two-level v0.3 fixture. It gave correct EPSG:32618, transforms and all six oracle values, without warnings. It did so both with and without `_CRS`. Selected raster slices did not expose automatic overviews.

CarbonPlan zarr-layer 0.10.0 with zarrita 0.7.5 rendered the fixture. It needed no CRS, bounds or spatial-dimension overrides. It returned 1107 at the checked pixel centre.

Separate-mask handling and framebuffer color calibration were not established.

Full reader evidence: [geozarr-profile.md](geozarr-profile.md#8-reader-spike).

## Layout choice

The writer default is unsharded: one object per chunk. A chunk is one cell, one level and one timestep. The 117-month imagery store has about 5,900 objects. There is no shard index to read. A CDN miss costs one chunk. An append writes only new objects.

A sharded store (`--shard`, `shard_time`) has 93 objects for the same data. A reader needs one range read per timestep once the shard index is cached. A CDN miss costs time proportional to the shard size.

Every store written sharded stays valid.

Why the default changed: a cold open of the published sharded store spent 6.5 of 6.8 s on the nine shard-index reads. A CDN miss on a 2 KB range at the end of an 83 to 174 MB shard pulls the whole object. An append to a sharded store rewrites the trailing shard.

Details of the same measurement are in [comparisons.md](comparisons.md).

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

The tables in this section used earlier format versions. They are dated evidence, not v0.3 release gates.

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

Appending one month to a 12-month Ucayali store (4 bands, 36 cells at level 0). A re-encode of the same store took 32 s and wrote 6.3 GB. 2026-10-01. Full tables: [archive/append-v02.md](archive/append-v02.md).

| Layout | Append wall time | Bytes written per month | Rewritten |
|---|---:|---:|---|
| Unsharded (default) | 0.6 s | 55 MB | metadata only |
| Yearly time shards | 0.6 s | 55 to 660 MB, 4.4 GB over a 12-month cycle | the trailing shard, whole |
| Whole-axis shard (`--shard`) | 0.6 s | 55 MB, then 111 MB and growing | a second shard that grows every month |

These are v0.2 measurements. [append.md](append.md) states they are historical and not a new v0.3 timing.

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

## Live store and publishing

The demo catalog points to `ucayali_santa_maria_v03` (v0.3, unsharded, 5,893 objects) and the v0.3 PNG store `ucayali_santa_maria/png-v03`. Both are on `data.chronozarr.org`. The imagery store has 117 monthly Sentinel-2 composites and 6,451,772,327 bytes.

History of the imagery prefix:

- `ucayali_santa_maria/chronozarr-3` was sharded, 93 objects. It is historical.
- `ucayali_santa_maria/chronozarr-4` was plain encoding, unsharded, 5,893 objects. It replaced `chronozarr-3` on 2026-10-01. The values are bit-identical.
- `ucayali_santa_maria_v03` replaced `chronozarr-4` in the catalog.
- The suffixes identify dataset revisions, not format versions. `chronozarr-3` and `chronozarr-4` are both spec v0.2.0.
- The v0.2 stores (`chronozarr-4`, `png-1`) are historical. v0.3 readers reject v0.2 stores. Use `chronozarr convert` or a pinned v0.2 reader for them.

Doctor results:

- `chronozarr-4` upload check: 13 ok, 0 failures. The check is a recorded result, not a fresh deployment verification.
- `chronozarr-2` on 2026-09-30: 15 ok, 2 info, 0 warnings. `edge cache` was HIT, `timing-allow-origin` was `*`, `cache-control` was `max-age=31536000`. This is historical host evidence, not a fresh check of the current prefix.

The host is R2 with `deploy/r2-cors.json` and the hostname rules in [hosting.md](hosting.md#32-cloudflare-r2).

Publishing times through the R2 S3 API with `scripts/r2_sync.py`:

- The 5,893 objects of `chronozarr-4` took 274 s.
- The 17,088 objects of the water store took 150 s.

Publishing through wrangler starts in about 2 seconds per object. At 4 parallel uploads, the 5,893-object store takes about 50 minutes. The 17,088-object water store takes 2.4 hours. A sharded store of around 100 objects takes a minute or two.

## Hosting observations

### Doctor

The `doctor` checklist in [hosting.md](hosting.md#1-checklist) was read from `src/chronozarr/doctor.py` on 2026-10-01. If that file changes, the file is the authority.

### Cloudflare cache rule

Verified on `data.chronozarr.org` on 2026-09-30: `zarr.json` returned MISS then HIT with an `age` header. A `206` range request on a shard returned MISS then HIT.

Overriding both TTLs is safe only because prefixes are immutable.

Setting `Cache-Control` on each object at upload still matters for clients of the bare bucket and for any rule that respects the origin.

A Cache Rule whose expression was pasted into a URI wildcard value matches nothing. R2 responses then stay `DYNAMIC` with no `Timing-Allow-Origin`. The dashboard warning that the rule "may not apply to your traffic" for the R2 hostname is a false alarm.

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

## Comparison notes

The tool-by-tool table is in [format-comparison.md](format-comparison.md). The old README summarized three tools:

- PMTiles packs tiles, images or vectors, for one moment. Its tile scheme is Web Mercator in practice. It has no native time axis. Per-tile values are whatever the image encoding carries.
- Mapbox raster-array (MRT) is multi-band numeric tiles with a time series. Its decoder code is published in mapbox-gl-js (`src/data/mrt`). The format is tied to Mapbox's tiling service and renderer.
- CarbonPlan ndpyramid and zarr-layer put Zarr pyramids in MapLibre with a time selector. zarr-layer supports arbitrary CRS through proj4 reprojection. Each timestep is its own chunk fetch. The v0.3 fixture opens without metadata overrides (see Reader checks).

chronozarr keeps native projection and lossless values. It serves a timestep as one plain `GET` of one chunk. It pre-stages a window of the time axis in the client, so a timestep switch costs zero bytes on the wire once cached.

## JavaScript dependencies

- The package is the ES modules under `js/chronozarr/` and `js/maplibre/`, published as they are. There is no build step and no runtime dependency.
- `cd js && npm install` installs only the test tooling.
- zarrita and its codecs are vendored under `js/vendor`: zarrita 0.7.5, @zarrita/storage 0.2.0, numcodecs 0.3.2, all MIT.
- The viewer has no runtime third-party host. Each vendored file header records its version, license and the SHA-256 of the published file. The page loads no web font.
- The MapLibre demo page `js/maplibre/index.html` is the exception by design. It loads maplibre-gl from a pinned CDN version.
- Usage examples: [js/README.md](../js/README.md).
- The checkout prepares `chronozarr` 0.3.1 for PyPI and npm. Both packages publish from one `v*` tag.
- `examples/sentinel2_pc/ingest.py` records band metadata, a 0/1 coverage plane and provenance in the store. Its `--stac` flag also writes a static STAC Collection and Item.

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

The architecture checker and `sentrux check .` share `.sentrux/rules.toml`. First-party imports must be acyclic. Shared writer and store modules must not depend on their callers. MapLibre and shared rendering must not depend on the demo.

The dependency-free checker runs in CI. It includes deferred Python imports and static JavaScript imports and re-exports. Computed runtime imports are outside its scope.

Python coverage measures every package module. It writes JSON and XML to `data/reports/coverage/python/`.

Native Node coverage measures only modules loaded by the Node tests under `chronozarr`, `shared`, `maplibre` and `demo`. It does not measure the browser-only viewer or GPU execution. Browser tests verify those behaviors separately.

CI retains both coverage reports for 14 days.

For a before and after speed check on the same local fixture:

```bash
node scripts/audit_browser.mjs js data/spike/stress6x6 data/reports/browser-bench.json
```

The script uses headless Chromium with software WebGL, three cold runs and 20 switches. Its additional `warmFullyLoaded` measurement primes all timesteps with a 2 GiB cache, because the ordinary idle prefetch intentionally stops at 64 MiB. Inspect complete frames and bytes alongside timings. Local and loopback results do not measure CDN performance.

## Stale text found in the old README

The old README Status section began "v0.2 draft (spec version `0.2.0`; every v0.1 store is a valid v0.2 store and readers accept both)". The same README, [spec/CHRONOZARR.md](../spec/CHRONOZARR.md) and `pyproject.toml` describe v0.3.0 and package 0.3.1. The new README states v0.3.
