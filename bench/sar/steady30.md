# NISAR native playback at 30 frames/s within default caches

The initial-buffering timings below are historical. The subsequent [startup
optimization](startup.md) moves lossless packing to decoder workers; refer to
that report for current ordinary-Play startup measurements.

The strict workload is the entire NISAR scene at native LOD 0: 72 cells,
26 timesteps, 12 consecutive loops (312 timestep advances, over 10 seconds at
30 frames/s). No timestep skips, partial frames, coarser fallback, cache-budget
changes or browser HTTP cache are permitted. Each repeat launches fresh Chromium
with the Metal GPU backend. Static data are local, with warm operating-system
file caches; these results do not establish performance over a slow remote link.

The unchanged budgets are 1536 MiB joint imagery (decoded plus compressed),
153.6 MiB auxiliary, and 384 MiB GPU textures. Cache samples are taken every
50 ms; these are resident cache accounting, not total process memory or temporary
fetch/decode allocations. Exact GPU texture allocation is reported separately.
Initial full-loop prefetch and cold frame are recorded separately rather than
excluded from the raw results. Playback success requires at least 29.5 actual
successive timestep advances/s, zero refill pauses, continuous timesteps and
complete native 72-cell paints. Cadence distributions and loop rates are retained.

## Baseline

The immutable production snapshot is
`/tmp/chronozarr-nisar-steady30-baseline`. Baseline results:

- 14.16 frames/s; 16 refill pauses totaling 11.60 seconds across 12 loops.
- 4.27-second explicit prefetch; zero additional initial buffering.
- 4.96 GB of playback traffic, including 4,643 duplicate imagery requests.
- Paint upload p95 3.10 ms and render p95 7.70 ms, no main-thread long tasks.
- Native frame intervals p95 598.4 ms, maximum 1033.4 ms.

Foreground rendering was already fast enough; retained imagery capacity and
repeated fetch/decode churn prevented steady playback. The full decoded loop is
1872 MiB, exceeding the imagery budget.

## Change under test

Lossless in-memory packing stores uint16 values as a minimum plus bit-packed
residuals in blocks of 128 values. Native values and on-disk chunks are unchanged.
The GPU samples packed values directly, avoiding CPU expansion during playback.
The viewer enables this path when the native imagery loop exceeds 90% of its
imagery budget; smaller loops retain the existing representation. NISAR's retained
imagery loop is about 1310.54 MiB, fitting the existing 1536 MiB cache without
streaming it again each loop. This is a cache representation change, not reduced
precision or a larger cache.

## Final measurements

Three fresh-browser repeats on the frozen implementation achieved **29.984 to
30.008 frames/s** (median 29.994). Each completed 12 loops, all 312 native
72-cell frames, with **zero refill pauses, skipped timesteps, partial frames,
overview fallback or playback network requests**. Per-loop rates ranged from
29.81 to 30.10 frames/s. Frame interval p95 was 33.5 to 34.1 ms; the maximum
interval across all three runs was 38.9 ms, with no interval over 50 ms.

The resident imagery peak was **1310.54 MiB**, auxiliary peak **93.44 MiB**,
and exact GPU texture allocation **383.09 MiB**. All remain within the existing
1536 / 153.6 / 384 MiB limits. The GPU accounting includes packed data,
block metadata and mask textures, including padding.

Initial full-loop preparation took **7.02 to 7.12 seconds**. Native cold frames
took **335.7 to 339.9 ms**. Playback then needed zero additional initial buffering.
The earlier rolling-buffer baseline prepared only a bounded portion in 4.27 s;
the new preparation retains the entire compact imagery loop. Preparation cost
is explicit, and the faster steady playback is not a faster-full-startup claim.

Final sources are recorded in every raw file; viewer `70124d5b`, decoder
`46ab9d88`, compact codec `17cb485b`, renderer `a73ece5f`, and cache policy
`d873bf7f` identify this implementation. Final records are
`results/steady30-final-frozen.jsonl`; the three earlier `after` repeats and
`candidate` pilot are development diagnostics, excluded from these final numbers.
The startup-only interrupted `final` record is also excluded.

A separate sustained run completed **60 loops / 1560 complete native frames**
over **51.99 seconds** at **29.997 frames/s**, with the same cache peaks and zero
refill pauses or playback requests. Frame intervals were p95 34.0 ms, maximum
40.6 ms, with zero intervals over 50 ms. Every per-loop rate remained between
29.77 and 30.03 frames/s. No page errors or main-thread long tasks occurred.
Its initial preparation took 7.12 seconds and cold native frame 336.5 ms. Raw
records are `results/steady30-sustained-final.jsonl`.

## Ordinary Play path

A separate fresh browser skipped the explicit harness prefetch and invoked the
ordinary viewer Play action. It initially buffered for **6.590 seconds**, then
completed 12 loops at **30.002 frames/s** with **zero refill pauses**. All 312
paints were complete native 72-cell frames; frame interval p95 was 34.2 ms,
maximum 34.9 ms, with no gaps over 50 ms or discontinuous timesteps. The Play
phase includes initial loading: 1800 imagery chunks and masks were fetched once
each (the cold frame already held 72), with no duplicate object requests. Its
final cache retained all 1872 imagery chunks. Thus ordinary Play reaches the same
steady behavior without requiring a separate prefetch call. Initial buffering is
reported separately from the subsequent frame rate. Raw record:
`results/steady30-direct-play-final.jsonl`.

## Regression and reader gates

The Sentinel-1 regression completed 330 native nine-cell frames over three loops
at **30.004 frames/s**, with zero refill pauses or playback requests. Frame
interval p95 was 34.3 ms and maximum 35.0 ms. Compact caching remained disabled
for this smaller loop.

The separate gate run measured per-chunk decode median/p95 **2.4/2.6 ms** for
Sentinel-1 and **2.5/2.7 ms** for NISAR. Complete cold native frames took 31.8 ms
and 280.2 ms respectively in that separate run. Sentinel-1's four ordinary warm
stepping and jumping switches completed with zero wire traffic. The generic gate
uses a bounded idle prefetch, not full movie preparation: three of four NISAR
stepping switches and all four jumping switches were incomplete and are retained
as failures, not claimed as passing warm-switch measurements. Sentinel-1's separate
forced-cache-upload variant also had two incomplete switches. Those arbitrary
switch diagnostics do not establish the prepared playback result above. Raw gates
are `results/steady30-gates.json`.

## Reproduction

```sh
BASELINE_DIR=/tmp/chronozarr-nisar-steady30-baseline CAP_MS=45000 node bench/sar/steady30.mjs baseline 1
CAP_MS=45000 node bench/sar/steady30.mjs final-frozen 3
SENSORS=sentinel1 LOOPS=3 CAP_MS=45000 node bench/sar/steady30.mjs sentinel1-final 1
LOOPS=60 CAP_MS=90000 node bench/sar/steady30.mjs sustained-final 1
SKIP_PREFETCH=1 CAP_MS=45000 node bench/sar/steady30.mjs direct-play-final 1
node bench/sar/gates.mjs > bench/sar/results/steady30-gates.json
node bench/sar/steady30-summary.mjs bench/sar/results/steady30-final-frozen.jsonl > bench/sar/results/steady30-final-frozen-summary.json
```

Raw records include source hashes, store-root hash, harness hash, network counters
including workers, actual frame LOD/cell count, timestep continuity, cache samples,
per-loop rates and long-task observations. The viewer's `movieInfo.lod` can
retain its automatic planning diagnostic (LOD 2 here) while the benchmark pins
LOD 0. Actual paint records and `shownFrame` are authoritative for what was
rendered: every paced paint in these final NISAR runs was LOD 0 with all 72 cells. The baseline harness predates the final provenance and GPU instrumentation additions. Its workload and cadence instrumentation are the same. The baseline excludes the
GPU allocation getter because the baseline renderer predates it. The successful
candidate pilot is diagnostic and excluded from the three final repeats.

## Verification

The final JavaScript suite passed 587 tests with zero skips. GPU parity and
allocation coverage passed for residual widths 0 through 16, odd shapes, raw
fallback, masks, three bands, constant zero-word chunks and padded texture storage.
Five targeted browser regressions and 13 Python tests passed. Viewer distribution
guards passed for 97 packed files and 91 reachable files. No release, upload or
deployment is part of this work.
