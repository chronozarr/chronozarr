# Performance and evidence

This page lists what was measured, how, on what, and what the measurements do not show. It has four parts: viewer speed on the published store, correctness of stored values, ingestion speed, and memory. They are kept apart because they rest on different data and different machines. Raw tables and dated observations are in [evidence.md](evidence.md).

All timings were taken on one computer, an Apple M3 Max laptop (16 CPUs, 128 GB, macOS 26.6.2), on a home connection. No other machine, browser vendor, operating system or network has been measured. Where a run was not repeated, the text says so.

## 1. Viewer performance (published store)

### The store

The public demo opens `https://data.chronozarr.org/ucayali_santa_maria_v03`: 117 monthly Sentinel-2 composites of the Ucayali River, Peru, from 2015-11 to 2026-02. Seven calendar months in that span have no timestep (2015-12, 2016-01 to 2016-03, 2017-04, 2017-06, 2022-02). Each timestep is a median composite, and pixels with no valid scene carry forward from the previous month.

| Property | Value |
|---|---|
| Grid | 2765 x 2759 pixels at 10 m, EPSG:32718, 4 bands (B02, B03, B04, B08), uint16 |
| Levels | 4 (level 0 at 10 m, level 3 at 80 m, one chunk) |
| Chunks | (1, 4, 512, 512), zstd level 5, one object each, no sharding |
| Objects | 5,893, of which 5,850 are chunks (63 KB to 1.8 MB, mean 1.1 MB) |
| Size | 6.45 GB (6.01 GiB) |
| Host | Cloudflare R2 behind Cloudflare's cache, `data.chronozarr.org` |

The numbers come from the store's root `zarr.json` on 2026-10-12 and from the local copy, which has the same root file. See [live store and publishing](evidence.md#live-store-and-publishing).

### Time to the first frame

**What was measured.** Time from opening the store in a fresh browser to the first frame that covers the whole view (at any pyramid level), and to the frame at the level the view asks for. The browser cache is empty or disabled in every run.

**Result.**

| Series | n | Whole view, after the root metadata has loaded (median, range) | Whole view, from the start of the open (median, range) | Frame at the requested level (median, range) |
|---|---|---|---|---|
| 2026-10-08, CDN warm, 60 Hz frames | 30 (4 cells) | 111 ms (85 to 368) | 318 ms (174 to 484) | 501 ms (273 to 816) |
| 2026-10-08, CDN warm, 60 Hz frames | 17 (9 cells) | 179 ms (86 to 338) | 302 ms (184 to 472) | 519 ms (342 to 980) |
| 2026-10-12, CDN warm, frames capped at 30 Hz | 10 | 343 ms (218 to 508) | 477 ms (319 to 726) | 744 ms (539 to 1046) |
| 2026-10-12, CDN cold, frames capped at 30 Hz | 10 | 404 ms (370 to 506) | 656 ms (548 to 758) | 981 ms (788 to 1109) |

The first frame downloads 11 requests and 11 MB (level 1, nine chunks, plus metadata). Reading the root metadata alone took 113 ms (warm) and 223 ms (cold), median.

An earlier figure of 390 ms (cold CDN, 2026-10-01) was one run in an application browser pane on a previous store, with the clock starting after the metadata read. It is consistent with the 404 ms above and is not used here.

**Method.** The 2026-10-08 runs come from a scrub A/B harness (`harness/scrub_ab.mjs`, raw records `scrub_ready_ahead_ab.json` and `scrub_ready_ahead_ab_rerun.json`, both in the gitignored `data/spike/scrub_ready_ahead/`): Playwright Chromium with Metal graphics, 1440 x 900 at device pixel ratio 2, the live store over the real network, a new browser per run, animation-frame median 16.7 ms. 77,498 of 77,774 counted responses were CDN hits. The 2026-10-12 runs use `bench/live/measure.mjs` (same browser setup, current deployed viewer, which was byte-identical to `main` that day). Raw runs: [cold open of the live store](evidence.md#cold-open-of-the-live-store-2026-10-12). The cold-CDN series adds a unique `?cb=` query to the store address, so every object is a cache miss (403 of 403). The laptop ran on battery at 14 %, which capped animation frames at 30 Hz, and the load average was 8 to 10 on 16 cores. Each reported paint can therefore be late by up to 33 ms.

**What this does not show.** Page, script and codec-module load are not in these times. One machine, one network, one CDN location (the Newark edge answered). No phone, no slow link, no other browser. The cold-CDN series is a synthetic miss, not a first visitor. The viewer changed after 2026-10-08 in ways that should not touch the open path, but only the 2026-10-12 series was taken on the deployed build.

### Decoding one chunk

**What was measured.** Time to decompress one stored chunk, 4 bands x 512 x 512 uint16 = 2,097,152 bytes (2 MiB) raw, 1.3 to 1.4 MB compressed, with zstd level 5 through zarrita's WebAssembly codec.

**Result.** 4.4 ms per chunk on one thread (median of 80 repeats, p95 4.6 to 4.9 ms), identical in five interleaved runs of two reader versions. Eight chunks decode in 9.6 ms on the worker pool, against 36.2 ms on the main thread. An earlier test with a busy main thread gave 10 ms.

**Method.** `chronozarr.bench()` replays recorded chunk bytes with no network. Headless Chrome 154, 1280 x 900, device pixel ratio 1, local server, 2026-10-03. Raw record: `data/spike/js_v03_bench.json`. Codec comparison on a Sahara chunk, 2026-09-29: zstd (WASM) 4.5 ms, native gzip 6.7 ms, pure-JavaScript zstd 14.1 ms. See [codec decode](evidence.md#codec-decode) and [v0.3 against v0.2](evidence.md#v03-against-v02).

**What this does not show.** The test decodes one chunk repeatedly with warm CPU caches. It does not time the chunk population (compressed sizes run from 63 KB to 1.8 MB), GPU upload, or other CPUs.

### Stepping and playback

**What was measured.** The rate at which the viewer steps through the 117 timesteps with the series already in memory, and how it behaves when it is not.

**Result.**

| View | State | Steps per second | Source |
|---|---|---|---|
| Whole area at 40 m (level 2, 4 chunks), 60/s requested | series in memory | 60.03 achieved, 0 holds | 2026-09-30, 120 Hz display, one 2-loop run |
| Same view | cold start | 20.5 over two loops (12.4 in the first, 60.0 in the second) | same |
| 20 m overview (level 1, 9 chunks, 11.7 MB per step), 60/s requested | cold | 6.0, link-limited at 55 to 60 MB/s | same |
| Same view | series in memory | 12.8 (the cache then held about 48 of 117 steps) | same |
| Whole area at 40 m, 60/s requested, current viewer | series in memory | 30.0 achieved, equal to the 30 Hz frame cap, 0 holds, 0 partial frames, 0 requests | 2026-10-12, two runs |
| Local store, 36 level-0 cells, one timestep at a time | series in memory | step time 4.9 ms median (v0.2), 4.5 ms (v0.3); p95 5.8 ms | 2026-10-03, five interleaved runs |

The viewer clamps its speed to the display's refresh rate, so 60 steps per second needs a display of 60 Hz or more. Since 2026-10-01 it also shows each frame whole or not at all, buffers two seconds before it starts, and drops to a coarser level when the loop does not fit in memory (cache cap 1.5 GiB, or 768 MiB on machines with under 8 GB). With a view that does not fit, 10 steps per second were achieved on the natural link and 7.8 at 50 Mbit/s and 40 ms (2026-10-01, [whole frames](evidence.md#whole-frames-and-buffered-playback)).

**Method.** `chronozarr.playBench({ stepsPerSecond, loops, lod, warm })` in the viewer console; headless Chromium, 1440 x 900 at pixel ratio 2. See [playback](evidence.md#playback).

**What this does not show.** The 60-per-second rows are one run each from before the whole-frame, cache and v0.3 changes. They have not been repeated on a 60 Hz display since. "No downloads" holds during the loop, not during the prefetch before it. The warm step times come from a local server, so they exclude network and CDN effects, and some of the sampled frames in that benchmark were incomplete (the jump series had 26 to 28 of 30; the step series 0 to 1).

### Comparison with other readers

No head-to-head timings are published. A local benchmark of this viewer, CarbonPlan's zarr-layer and COG-per-date through TiTiler exists (`bench/v03`, 2026-10-09). Its summary is marked provisional: the machine was shared, the servers ran on localhost over HTTP/1.1, and no CDN was involved. It shows that the viewer revisits a date it has cached in 2 to 5 ms, and that over 50 and 10 Mbit/s links its step time at the default (finer) overview level is longer than the other two, because it fetches more bytes per step. Results with matched view resolution are closer. Until the full experiment is run on a quiet machine with a CDN, this page makes no claim of being faster than another tool. Capability statements about other tools are in [format-comparison.md](format-comparison.md) and are dated in [comparison notes](evidence.md#comparison-notes).

## 2. Scientific correctness of stored values

**What was checked.** Whether the numbers a reader returns equal the numbers that were written, and whether changes to the writer or ingest code change any stored value.

| Check | Result | Backing |
|---|---|---|
| Encode, then read every timestep and level | identical | `tests/test_roundtrip.py`, `tests/test_xarray_compat.py`, `tests/test_reads.py` |
| Level 0 holds the source values; each coarser level holds 2 x 2 integer means of the level below, with nodata excluded | as specified | `tests/test_roundtrip.py::test_downsample_excludes_nodata_and_keeps_all_nodata_blocks` |
| `convert` of GeoTIFF or COG manifests keeps values, nodata, masks, scale and offset | identical on tests; exact level-0 values and masks on 3 real dates (905 x 741) and on 12 real dates (1136 x 1107) with chronozarr 0.2.1 | `tests/test_convert.py`, `examples/bring_your_data/results/` |
| Public Ucayali store against the monthly files it was built from | all 117 timesteps equal bit for bit (local copy); 5,928 objects matched by ETag and size on upload | [Ucayali scene audit](evidence.md#ucayali-scene-audit), [live store](evidence.md#live-store-and-publishing) |
| JavaScript reader against Python | 0 mismatches in 18 pixel-date samples across five sources and in 3 displayed pixels (2026-10-09); `chronozarr doctor` decodes cells at every level and compares one plain-Zarr pixel | `bench/v03/values.mjs`, `js/test/decoder*.test.js` |
| Sentinel-2 monthly composites do not depend on settings, strip height or concurrency | identical hashes in every record | `tests/test_ingest_mosaic.py` (strips, warps, resume), [spatial strips](evidence.md#spatial-strips) |
| Composites against the previous ingest code | identical for scenes on the AOI's own UTM grid, in all 137 benchmark runs; not identical for scenes from another UTM zone, where the SCL read is now exact (0.18 % of values differ, median 1 DN; 0.05 % of pixels change their set of valid scenes) | [spatial strips](evidence.md#spatial-strips) |
| An interrupted ingest resumes to the same files | identical | `tests/test_ingest_mosaic.py::test_a_run_interrupted_inside_a_month_resumes_to_the_same_files` |
| Time-slab encode against single-pass encode | byte-identical in 14 test stores and in the 117-month store (11,574 files) | `tests/test_encode_slabs.py`, [integration check](evidence.md#integration-check-2026-10-12) |
| Coverage counts rebuilt from legacy fractions | accepted only if every value reproduces bit for bit with the smallest scene count | `tests/test_ingest_encode.py`; all 117 Ucayali months pass |

**Method.** Unit tests run on every change (`uv run pytest -q -m unit`, `cd js && node --test`). Real-data checks compare SHA-256 hashes or every value with an independent read (zarr-python, rasterio, pyproj).

**What this does not show.** Pixel comparisons between the JavaScript reader and Python cover a sample of pixels, not every value. The real-data conversion checks predate the v0.3 writer. The published store's `coverage` is a 0/1 flag until it is re-encoded with the count writer. "True values" means unquantized stored numbers: level 0 is the source data, and coarser levels are means. When this page was drafted, `tests/test_roundtrip.py`, `tests/test_encode_slabs.py` and `tests/test_xarray_compat.py` were run (128 passed, 2026-10-12). The other tests listed were read, not run.

## 3. Ingestion throughput (Sentinel-2 example)

**What was measured.** Wall time to build monthly median composites from Planetary Computer, with the new download pipeline against the previous one (`mosaic.py` at commit `01185cd`).

**Result.** Median of 3 interleaved runs per cell, 2026-10-10:

| Workload (scenes) | Previous | New default | Speed-up | Peak process memory, previous to new |
|---|---|---|---|---|
| Ucayali, 1 month (10) | 44.1 s | 20.9 to 22.8 s | 1.9 to 2.1 | 10.6 to 2.7 GB |
| Ucayali, 5 km box, 3 months (15) | 19.6 s | 4.2 to 4.3 s | 4.6 to 4.7 | 0.39 to 0.27 GB |
| Ucayali, scenes from another UTM zone (10) | 45.3 s | 20.2 to 22.5 s | 2.0 to 2.2 | 10.9 to 2.9 GB |
| Lake Mead, 1 month, 4 tiles (16) | 34.9 s | 14.0 s | 2.5 | 12.7 to 2.9 GB |
| Yukon delta, 3 sparse months (11) | 145.4 s | 48.4 s | 3.0 | 14.5 to 4.6 GB |

Every output month was bit-identical to the previous pipeline's. Against a local server with no traffic shaping, the 20-scene Ucayali month took 2.3 s against 18.1 s (7.9 times; 16.9 to 4.3 GB). With a 10 MB/s cap the gain is 1.3 times (88 s against 115 s). Peak memory fell 3.2 to 4.4 times on the four full-size workloads.

**Method.** Frozen scene lists, a fresh process per run, configurations interleaved in random order, requests and bytes from GDAL's own network counters, peak memory from `ru_maxrss`. "New default" is `auto` or `fixed-16`, which are the same when no read fails; Lake Mead and Yukon were run with `fixed-16`. Hardware as above, GDAL 3.12.1 through rasterio 1.5.0, Python 3.13. See [method](evidence.md#method), [Planetary Computer over a home connection](evidence.md#planetary-computer-over-a-home-connection) and [local server with traffic shaping](evidence.md#local-server-with-traffic-shaping).

**What this does not show.**
- Three runs per cell. The `auto` run on Ucayali had an upper quartile of 36.2 s. No confidence intervals.
- The 4.7 times case is a 5 km box that downloads 52 MB.
- The home link delivered 12 to 18 MB/s at any concurrency, and Planetary Computer throughput varied between sessions by more than the lower bound. Fourteen later runs of the 1-month workload ranged from 11 s to 55 s, and the slowest hit DNS failures.
- The speed-ups were measured on the code of 2026-10-10. The merged code (strips, GeoTIFF month files, exact SCL read) was benchmarked against that code on a local server (1.6 s against 2.9 s for 20 scenes) and not against the original on Planetary Computer. Months with scenes from a neighbouring UTM zone are slower by the cost of the exact SCL read (6.4 s against 4.0 s).
- The previous pipeline's memory includes GDAL's default block cache, 5 % of RAM, which is 6.4 GiB on this 128 GB machine. The ratio would be smaller on a smaller machine.
- Only Planetary Computer was used. The largest run measured downloaded 1.3 GB. No workload of tens or hundreds of gigabytes has been run or claimed.
- Not tested on x86 Linux, a SLURM node or a cloud VM.

## 4. Memory scalability (encoder and strips)

### Encoder: time slabs

**What was measured.** Peak resident memory of the encode step for the 117 Ucayali months (4 x 2765 x 2759), and of synthetic series of different length.

**Result.**

| Encode path | Peak memory | Time |
|---|---|---|
| All months stacked in memory (PR #90 ingest) | 10.2 GB | 30.5 s |
| Months read lazily, one 512 x 512 cell of every month at a time (PR #95) | 3.3 GB | 56.6 s, of which 29.9 s is the one-time copy of legacy `.npz` months to GeoTIFF |
| Lazy read and time slabs of about 64 MiB per cell (PR #99) | 1.34 GB | encode step 32.6 s (26.7 s before slabs) |

The three stores are byte-identical (11,574 files). Synthetic, 2048 x 2048 x 4 uint16, lazy input: 1,282 MB at 32 timesteps, 1,305 MB at 128 and 1,310 MB at 256 with slabs, against 1,285, 4,139 and 7,926 MB before. Through the GeoTIFF lazy reader, 1024 x 1024 x 4:

| Months | Time slabs | One slab | Sharded output |
|---|---|---|---|
| 60 | 626 MiB | 889 MiB | 921 MiB |
| 120 | 654 MiB | 1,668 MiB | 1,572 MiB |
| 240 | 825 MiB | 2,693 MiB | 2,838 MiB |

**Method.** Fresh process per run, peak RSS, one run per cell. Source: the descriptions of the merged pull requests #95 and #99 and [spatial strips](evidence.md#spatial-strips).

**What this does not show.** Memory with slabs still grows slowly with the series, about 1 MiB per month here against 10 MiB in one slab. Sharded stores and `append` still grow with the series. The slab code is on `main` after the 0.4.0 release and is not in the published 0.4.0 package. The raw run output is not kept in the repository and no script reproduces the tables; the unit tests check byte identity and, with a Python allocation tracer, the growth.

### Ingest: spatial strips

**What was measured.** Whether the download pipeline completes a month under a hard memory limit, by compositing it in full-width strips of the grid.

**Result.** The workload is one month of 20 scenes (10 real scenes, each listed under two addresses), 2765 x 2759 pixels, 4 bands, served from a local mirror.

| Container limit | Previous path | Strips |
|---|---|---|
| 1 CPU, 1.5 GB | killed by the out-of-memory killer, or refused before download | completed in 3 of 3 runs, 6 strips of 512 rows, 16.4 to 17.3 s, process peak 0.71 to 0.74 GiB, container peak 0.86 to 0.89 GiB |
| 2 CPUs, 2 GB | refused | completed in 4 of 4 runs, 3 strips, 7.4 to 7.8 s, process peak 0.87 to 0.90 GiB |
| 1 CPU, 2 GB, 4 months | not run | completed, 1.17 GiB, 30.5 s |
| 1 CPU, 1.5 GB, scenes from another UTM zone | not run | refused before download (needs 1.09 GiB, budget 1.06 GiB) |
| 1 CPU, 1 GB | not run | refused before download (needs 0.98 GiB) |

Strips cost bytes: a source block cut by a strip edge is read once per strip, 18 % more bytes at 1024 rows and 55 % more at 512 rows. On an uncapped link, strips of 1024 rows cost no measurable time. The memory estimate was 7 to 45 % above the measured process peak in every run. Output hashes equalled the whole-grid result's in every record.

**Method.** `bench/s2-ingest/constrained.sh`: Docker Desktop VM on the laptop (Linux aarch64, 7.65 GiB), swap off, `--cpus` and `--memory` set per run. See [spatial strips](evidence.md#spatial-strips) and `bench/s2-ingest/portability.md`.

**What this does not show.** Docker on one laptop only. A local server, not Planetary Computer, under a memory cap. No x86 Linux, SLURM node or cloud VM. A strip spans the full width of the grid, so a very wide area on a small budget must be split by the user. The encode step was not run under the same limits.

## How to reproduce

Viewer, in the browser console of [the live demo](https://chronozarr.org/demo/) or of a local viewer:

```js
await chronozarr.bench()                                  // cold open, warm switch, product switch, decode
await chronozarr.playBench({ stepsPerSecond: 60, loops: 2, lod: 2, warm: 'prefetch' })
await chronozarr.scrubBench()
await chronozarr.interactionBench()
```

Headless, with software WebGL, against a local fixture: `node scripts/audit_browser.mjs js data/spike/stress6x6 data/reports/browser-bench.json` ([CONTRIBUTING.md](../CONTRIBUTING.md)). Codec sizes and decode: `js/support/codec-bench/` and `scripts/bench_codecs.py`. Delivery comparison with zarr-layer and COG: `node bench/v03/run.mjs` (several hours; needs Docker and a local copy of the store; see [bench/v03/README.md](../bench/v03/README.md)). Value check against independent readers: `node bench/v03/values.mjs`.

Ingestion, from the repository root:

```sh
uv run python examples/sentinel2_pc/bench.py freeze --workload ucayali-1m
uv run python examples/sentinel2_pc/bench.py compare --workloads ucayali-1m --configs baseline,fixed-4,fixed-16,auto --reps 3
uv run python examples/sentinel2_pc/bench.py summarize
uv run python bench/s2-ingest/analyze.py all
bench/s2-ingest/constrained.sh --cpus 1 --memory 1500m --tag NAME -- --workload lab-ucayali-x2 --config auto --rep 0
uv run python bench/s2-ingest/e2e_check.py OUT_DIR       # real-data check of strips, counts and time slabs
```

The full command list, with the mirror and lab-server steps, is in the [Sentinel-2 example](../examples/sentinel2_pc/README.md#benchmark). `bench.py` records the machine with every result.

Correctness:

```sh
uv run pytest -q -m unit tests/test_roundtrip.py tests/test_encode_slabs.py tests/test_ingest_mosaic.py tests/test_ingest_encode.py tests/test_convert.py
cd js && node --test
uv run chronozarr doctor https://data.chronozarr.org/ucayali_santa_maria_v03
```

Memory of the encode step has no script yet. A run under `/usr/bin/time -l` of `chronozarr convert` on a directory of GeoTIFFs gives the peak resident size.

Raw tables, dates and caveats for everything above are in [evidence.md](evidence.md).
