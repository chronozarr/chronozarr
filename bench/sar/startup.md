# NISAR native playback startup

The direct-Play benchmark opens a fresh browser, pins the entire NISAR scene at
native LOD 0 (72 cells), and presses Play at 30 frames/s without an explicit
prefetch. It records initial buffering separately from subsequent playback over
12 loops / 312 consecutive frames. The unchanged budgets are 1536 MiB imagery,
153.6 MiB auxiliary and 384 MiB GPU texture storage. Cache samples reflect
resident accounting, not browser process memory or temporary allocations.

Sources before these changes are preserved at
`/tmp/chronozarr-nisar-startup-baseline`. Three baseline runs initially buffered
for 6.765, 6.933 and 6.943 seconds, then sustained 30.001 frames/s without refill
pauses. The separate baseline CPU profile is diagnostic and excluded from these
three timings. Approximately 3.00 seconds of its initial 7.11-second buffering
interval were sampled inside main-thread `packUint16`, and 0.75 seconds in
`packAux`. Sampling is approximate, but this makes packing a concrete startup
bottleneck rather than attributing the wait to rendering.

The worker-packing change moves lossless packing into decoder workers and
returns the packed representation directly to the reader. Its first diagnostic
run reduced initial buffering to 3.678 seconds while maintaining 30.001 frames/s,
zero refill pauses and complete native frames. This pilot is excluded from final
comparison statistics.

Chunk records retain fetch/decode timestamps, although their durations include
limiter and worker queue waits rather than pure network and codec time. Network
counters sampled at the first timestep isolate subsequent steady traffic. CPU
profiles are separate diagnostic runs, avoiding profiler overhead in the final
comparison. Raw records retain production-source, store-root and harness hashes,
frame coverage, cadence, continuity and simultaneous cache samples.

This is local Chromium/Metal playback with warm operating-system file caches,
not a remote-host bandwidth guarantee. No package, store or site is released.

## Final results

Three fresh browsers using ordinary Play, without manual prefetch, reduced
initial buffering from **6.933 seconds median to 3.661 seconds median**:
**47.2% less waiting**. The native first frame improved from 360.0 ms median to
223.6 ms median. Playback uses the entire 72-cell scene and all 26 dates.

| Measurement | Baseline, three runs | Final, three runs |
|---|---|---|
| Initial buffering | 6.765–6.943 s | **3.652–3.687 s** |
| Complete native cold frame | 355.9–363.2 ms | **221.1–229.7 ms** |
| Subsequent frame rate | 30.001 fps | **30.001 fps** |
| Refill pauses | 0 | **0** |
| Steady requests / bytes after first advance | 0 / 0 | **0 / 0** |

Every final run completed 312 consecutive complete native 72-cell frames across
12 loops. There were no skipped timesteps, partial paints, overview fallback or
page errors. Frame interval p95 was 34.1 ms, maximum 39.6 ms, with no gaps over
50 ms. Cache peaks stayed **1310.54 MiB imagery**, **93.44 MiB auxiliary** and
**383.09 MiB GPU textures**, within the unchanged defaults. All 1872 imagery
chunks remained resident after initial loading.

The initial Play phase fetched the remaining 1800 imagery chunks and 1800 masks
once each; the cold frame already held 72. It transferred about 1.37 GB of imagery
plus 0.73 MB of masks (page accounting). Reader counters include worker
traffic; the snapshot at the first advance and final snapshot are identical,
establishing **zero subsequent requests or wire bytes**. No object path repeated.
Initial loading cost remains explicit rather than being counted as steady playback.

Final source fingerprints in raw records include viewer `8a1912b9`, decoder
`1486cc37`, worker pool `7164211f`, decoder worker `72215384`, compact codec
`73aebf6a` and cache `eb79499e`. Paint events and `shownFrame` verify LOD 0;
`movieInfo` retains automatic LOD 2 planning diagnostics despite the pinned native
view. Final records are `results/startup-final.jsonl`; pilots and the separate
CPU profile are retained and excluded from the three-run timing comparison.

A separate sustained ordinary-Play run initially buffered for **3.660 seconds**,
then completed **60 loops / 1560 complete native frames** over **51.965 seconds**
at **30.001 frames/s**. It had zero refill pauses, steady requests/bytes, skipped
or partial frames, and overview fallback. Frame interval p95 was 34.1 ms,
maximum 35.9 ms, with no gap over 50 ms. Per-loop rates were 29.964–30.066 fps.
The cache limits and peaks were unchanged. Raw sustained results are
`results/startup-sustained-final.jsonl`.

## Rejected scheduling experiment

A three-lane continuous frame loader replaced the existing bounded batches in one
pilot. Initial buffering was 3.844 seconds, compared with the worker-only pilot's
3.678 seconds, and it introduced six duplicate imagery requests. An overlapping
short unit check makes the speed difference uncertain, but there was no measured
benefit and the extra traffic was concrete. The scheduling experiment was reverted;
its `startup-lanes-pilot.jsonl` record is retained and excluded from final results.
The final implementation keeps the original three-frame bounded loader.

## Reproduce

```sh
BASELINE_DIR=/tmp/chronozarr-nisar-startup-baseline CAP_MS=45000 node bench/sar/startup.mjs baseline 3
PROFILE=1 BASELINE_DIR=/tmp/chronozarr-nisar-startup-baseline CAP_MS=45000 node bench/sar/startup.mjs baseline-profile 1
CAP_MS=45000 node bench/sar/startup.mjs final 3
LOOPS=60 CAP_MS=90000 node bench/sar/startup.mjs sustained-final 1
SENSORS=sentinel1 LOOPS=3 CAP_MS=45000 node bench/sar/startup.mjs sentinel1-final 1
node bench/sar/startup-summary.mjs bench/sar/results/startup-final.jsonl > bench/sar/results/startup-final-summary.json
```

## Sentinel-1 regression

Ordinary Sentinel-1 Play initially buffered for 1.286 seconds, then sustained
30.001 frames/s across 330 complete native nine-cell frames and three loops,
with zero refill pauses. Frame interval p95 was 34.0 ms, maximum 36.3 ms, with
no gaps over 50 ms or page errors. Compact imagery remained disabled.

This regression deliberately skips manual prefetch. Sentinel-1 starts before
all 110 dates are fetched, so loading continues during its early playback:
238 MB of reader traffic followed the first advance. Across the Play phase,
24 imagery and 19 mask requests repeated paths. Those counts are retained,
and this run does not claim zero Sentinel-1 traffic or duplicate requests.
The earlier explicitly prefetched Sentinel-1 test in `steady30.md` had zero
playback traffic. NISAR's final runs above retained the full native timeline
before the first advance and had no repeated imagery or mask paths.
Raw regression results are `results/startup-sentinel1-final.jsonl`.

## Verification

Final JavaScript unit tests: 590 passed, zero failed or skipped. Seven browser
checks passed, including real worker-to-reader-to-viewer exact CPU values and
masked GPU frames, all packing bit widths, raw fallback, dual-polarization display,
mask behavior and repeated store replacement. The package reachability guard
passed (97 files, 91 reachable); whitespace checks passed. Nothing was released,
committed, pushed or deployed.
